"""Fixed-feed company-IR projection for source monitoring.

The hash in this adapter is a deterministic projection of normalized RSS item
fields.  It is explicitly not a hash of the linked announcement web page.  The
wrapped four-feed adapter is injectable, and this module performs no storage,
provider, model, or trading operation.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from ...market.ir_releases import IR_FEEDS, OfficialIrReleaseAdapter
from ...source_inbox_contracts import (
    PROJECT_SOURCE_ITEM_VERSION,
    canonicalize_source_url,
)
from ..contracts import (
    MAX_OBSERVED_ITEMS_PER_POLL,
    MAX_SOURCE_ERRORS_PER_POLL,
    AdapterPollResult,
    SourceMonitoringContractError,
    SourcePollError,
    canonical_sha256,
    normalize_checkpoint,
)
from .base import (
    MAX_POLL_INTERVAL_MS,
    MIN_POLL_INTERVAL_MS,
    SOURCE_ADAPTER_CONTRACT_VERSION,
    validate_poll_context,
)


COMPANY_IR_ADAPTER_KEY = "company_ir"
COMPANY_IR_CHECKPOINT_VERSION = "company_ir_checkpoint_v1"
COMPANY_IR_CONFIG_BASIS_VERSION = "company_ir_config_basis_v1"
COMPANY_IR_IDENTITY_VERSION = "company_ir_identity_v1"
COMPANY_IR_RSS_PROJECTION_VERSION = "company_ir_rss_projection_v1"
COMPANY_IR_POLL_INTERVAL_MS = 5 * 60 * 1_000
MAX_IR_PROJECTIONS = 250

_IR_EVENT_TYPES = frozenset({
    "earnings_schedule",
    "earnings_release",
    "earnings_material",
    "other",
})

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class _IrBatchAdapter(Protocol):
    """The deliberately small injected dependency used by this projection."""

    def recent_releases_batch(
        self,
        symbols: tuple[str, ...] | list[str],
        *,
        limit: int = 8,
        force: bool = False,
    ) -> dict[str, Any]: ...


def _checkpoint_error(message: str) -> SourceMonitoringContractError:
    return SourceMonitoringContractError(
        "COMPANY_IR_CHECKPOINT_INVALID",
        message,
    )


def _native_observed_at(value: Any) -> tuple[int, datetime]:
    if type(value) is not int or value < 0:
        raise SourceMonitoringContractError(
            "SOURCE_MONITORING_CAPTURE_TIME_INVALID",
            "observed_at_ms must be a non-negative native integer",
        )
    try:
        observed = datetime.fromtimestamp(value / 1_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise SourceMonitoringContractError(
            "SOURCE_MONITORING_CAPTURE_TIME_INVALID",
            "observed_at_ms is outside the supported UTC datetime range",
        ) from exc
    return value, observed


def _rfc3339(value: Any, *, observed_at: datetime) -> str:
    if type(value) is not str or not value.strip():
        return ""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        return ""
    parsed = parsed.astimezone(timezone.utc)
    if parsed > observed_at:
        return ""
    base = parsed.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f".{parsed.microsecond:06d}".rstrip("0") if parsed.microsecond else ""
    return f"{base}{fraction}Z"


def _normalize_checkpoint(
    value: Any,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    checkpoint = normalize_checkpoint(value)
    if checkpoint == {}:
        return checkpoint, [], {}
    if set(checkpoint) != {"version", "projections"}:
        raise _checkpoint_error("company IR checkpoint fields do not match v1")
    if checkpoint.get("version") != COMPANY_IR_CHECKPOINT_VERSION:
        raise _checkpoint_error("company IR checkpoint version is unsupported")
    raw_entries = checkpoint.get("projections")
    if type(raw_entries) is not list or len(raw_entries) > MAX_IR_PROJECTIONS:
        raise _checkpoint_error("projections must be a bounded native list")
    order: list[str] = []
    projections: dict[str, str] = {}
    for entry in raw_entries:
        if type(entry) is not dict or set(entry) != {
            "identity_sha256",
            "rss_projection_sha256",
        }:
            raise _checkpoint_error("projection entry fields are invalid")
        identity_sha = entry.get("identity_sha256")
        projection_sha = entry.get("rss_projection_sha256")
        if (
            type(identity_sha) is not str
            or not _SHA256_RE.fullmatch(identity_sha)
            or type(projection_sha) is not str
            or not _SHA256_RE.fullmatch(projection_sha)
            or identity_sha in projections
        ):
            raise _checkpoint_error("projection entry identity or hash is invalid")
        order.append(identity_sha)
        projections[identity_sha] = projection_sha
    return checkpoint, order, projections


def _source_errors(payload: dict[str, Any]) -> list[SourcePollError]:
    raw_errors = payload.get("source_errors")
    if type(raw_errors) is not list:
        return []
    errors: list[SourcePollError] = []
    for raw in raw_errors[:50]:
        if type(raw) is not dict:
            continue
        code = raw.get("code")
        message = raw.get("message")
        if type(code) is not str or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
            code = "IR_SOURCE_ERROR"
        if type(message) is not str or not message.strip():
            message = "company IR source returned an unspecified error"
        scope_value = raw.get("symbol") or raw.get("source") or "official_company_ir"
        scope = scope_value if type(scope_value) is str else "official_company_ir"
        errors.append(SourcePollError.build(code, message[:1_000], scope[:160]))
    return errors


def _official_url(value: Any, *, allowed_hosts: set[str]) -> str:
    if type(value) is not str:
        return ""
    try:
        clean = canonicalize_source_url(value.strip())
        parsed = urlsplit(clean)
    except (ValueError, TypeError):
        return ""
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        return ""
    return clean


def _clean_guid(value: Any) -> str:
    if type(value) is not str:
        return ""
    return " ".join(value.split())[:1_000]


def _identity_and_projection(
    *,
    symbol: str,
    guid: str,
    official_url: str,
    title: str,
    summary: str,
    published_at: str,
) -> tuple[str, str, str, str]:
    identity_kind = "guid" if guid else "url"
    identity_value = guid or official_url
    identity_sha = canonical_sha256({
        "version": COMPANY_IR_IDENTITY_VERSION,
        "symbol": symbol,
        "kind": identity_kind,
        "value": identity_value,
    })
    projection_sha = canonical_sha256({
        "version": COMPANY_IR_RSS_PROJECTION_VERSION,
        "symbol": symbol,
        "guid": guid,
        "official_url": official_url,
        "title": title,
        "summary": summary,
        "published_at": published_at,
    })
    return identity_kind, identity_value, identity_sha, projection_sha


def _release_item(
    *,
    symbol: str,
    publisher: str,
    feed_url: str,
    release: dict[str, Any],
    official_url: str,
    published_at: str,
    guid: str,
    identity_kind: str,
    identity_value: str,
    identity_sha: str,
    projection_sha: str,
    previous_projection_sha: str,
) -> dict[str, Any]:
    title = release["title"]
    summary = release["summary"]
    is_revision = bool(previous_projection_sha)
    event_type = release.get("event_type")
    event_type_text = event_type if type(event_type) is str else "other"
    fiscal_period = release.get("fiscal_period")
    fiscal_period_text = fiscal_period if type(fiscal_period) is str else ""
    return {
        "version": PROJECT_SOURCE_ITEM_VERSION,
        "external_item_id": f"ir-{identity_sha}",
        "item_type": "company_ir_release",
        "severity": "info",
        "occurred_at": published_at,
        "published_at": published_at,
        "entities": [{
            "kind": "security",
            "id": symbol,
            "label": symbol.removeprefix("US."),
        }],
        "headline": title[:500],
        "summary": summary[:8_000] or f"Official company IR release: {title}",
        "facts": [{
            "claim": f"{publisher} published the RSS item titled '{title}'.",
            "source_indexes": [0, 1],
        }],
        "sources": [
            {
                "url": official_url,
                "publisher": publisher,
                "source_type": "company_ir",
                "published_at": published_at,
                "content_sha256": "",
            },
            {
                "url": feed_url,
                "publisher": publisher,
                "source_type": "company_ir_rss_projection",
                "published_at": published_at,
                "content_sha256": projection_sha,
            },
        ],
        "impact_hypotheses": [],
        "unknowns": [
            "The linked announcement web-page body was not fetched or hashed."
        ],
        "confidence": 1.0,
        "recommended_route": "notify_only",
        "extensions": {
            "company_ir_v1": {
                "event_type": event_type_text,
                "fiscal_period": fiscal_period_text,
                "guid": guid,
                "identity_kind": identity_kind,
                "identity_value": identity_value,
                "identity_sha256": identity_sha,
                "is_revision": is_revision,
                "previous_rss_projection_sha256": previous_projection_sha,
                "rss_hash_semantics": "normalized_rss_item_not_web_page_body",
                "rss_projection_sha256": projection_sha,
                "rss_projection_version": COMPANY_IR_RSS_PROJECTION_VERSION,
            }
        },
    }


class CompanyIrSourceAdapter:
    """Project only the four code-defined official IR feeds into inbox items."""

    contract_version = SOURCE_ADAPTER_CONTRACT_VERSION
    adapter_key = COMPANY_IR_ADAPTER_KEY
    official_source = True
    execution_capability = "none"
    live_trading_allowed = False

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    @property
    def per_symbol_limit(self) -> int:
        return self._per_symbol_limit

    @property
    def max_candidates_per_poll(self) -> int:
        return len(self.symbols) * self.per_symbol_limit

    @property
    def force(self) -> bool:
        return self._force

    @property
    def poll_interval_ms(self) -> int:
        return self._poll_interval_ms

    @property
    def config_version(self) -> str:
        return self._config_version

    def _config_basis(self) -> dict[str, Any]:
        feed_snapshot = []
        for symbol in self.symbols:
            config = IR_FEEDS[symbol]
            feed_snapshot.append({
                "symbol": symbol,
                "publisher": config["publisher"],
                "url": config["url"],
                "hosts": sorted(config["hosts"]),
                "presentation_hub_url": config["presentation_hub_url"],
                "technology_scope": list(config["technology_scope"]),
            })
        return {
            "version": COMPANY_IR_CONFIG_BASIS_VERSION,
            "adapter_key": self.adapter_key,
            "checkpoint_version": COMPANY_IR_CHECKPOINT_VERSION,
            "identity_version": COMPANY_IR_IDENTITY_VERSION,
            "rss_projection_version": COMPANY_IR_RSS_PROJECTION_VERSION,
            "feeds": feed_snapshot,
            "per_symbol_limit": self.per_symbol_limit,
            "max_candidates_per_poll": self.max_candidates_per_poll,
            "inner_adapter_type": self._inner_adapter_type_token,
            "inner_transport_mode": self._inner_transport_mode,
            "force": self.force,
            "poll_interval_ms": self.poll_interval_ms,
        }

    def _assert_config_seal(self) -> None:
        if (
            self._adapter is not self._sealed_inner_adapter
            or (
                f"{type(self._adapter).__module__}."
                f"{type(self._adapter).__qualname__}"
            )
            != self._inner_adapter_type_token
        ):
            raise SourceMonitoringContractError(
                "COMPANY_IR_SOURCE_PROVENANCE_DRIFT",
                "company IR inner adapter changed after construction",
            )
        if self._seal_transport_identity and (
            getattr(self._adapter, "_fetch_bytes", None)
            is not self._sealed_inner_transport
        ):
            raise SourceMonitoringContractError(
                "COMPANY_IR_SOURCE_PROVENANCE_DRIFT",
                "company IR transport changed after construction",
            )
        expected = "company_ir_config_v1_" + canonical_sha256(
            self._config_basis()
        )[:16]
        if self.config_version != expected:
            raise SourceMonitoringContractError(
                "COMPANY_IR_CONFIG_DRIFT",
                "company IR adapter configuration changed after construction",
            )

    def __init__(
        self,
        *,
        adapter: _IrBatchAdapter | None = None,
        symbols: tuple[str, ...] | list[str] = tuple(IR_FEEDS),
        per_symbol_limit: int = 8,
        force: bool = False,
        poll_interval_ms: int = COMPANY_IR_POLL_INTERVAL_MS,
    ) -> None:
        if type(symbols) not in {list, tuple}:
            raise ValueError("company IR symbols must be a native list or tuple")
        selected: list[str] = []
        for raw_symbol in symbols:
            if (
                type(raw_symbol) is not str
                or raw_symbol not in IR_FEEDS
                or raw_symbol in selected
            ):
                raise ValueError(
                    "company IR symbols must be unique entries in the fixed feed map"
                )
            selected.append(raw_symbol)
        if not selected:
            raise ValueError("company IR symbols must remain inside the fixed feed map")
        if type(per_symbol_limit) is not int or not 1 <= per_symbol_limit <= 20:
            raise ValueError("per_symbol_limit must be a native integer from 1 to 20")
        if len(selected) * per_symbol_limit > MAX_OBSERVED_ITEMS_PER_POLL:
            raise ValueError(
                "company IR candidate bound must remain at or below 50 items per poll"
            )
        if type(force) is not bool:
            raise ValueError("force must be a native boolean")
        if (
            type(poll_interval_ms) is not int
            or not MIN_POLL_INTERVAL_MS <= poll_interval_ms <= MAX_POLL_INTERVAL_MS
        ):
            raise ValueError(
                "poll_interval_ms must be a native integer from one minute to seven days"
            )
        if adapter is not None and not callable(
            getattr(adapter, "recent_releases_batch", None)
        ):
            raise ValueError("adapter must implement recent_releases_batch")
        self._symbols = tuple(selected)
        self._per_symbol_limit = per_symbol_limit
        self._force = force
        self._poll_interval_ms = poll_interval_ms
        source_adapter = adapter or OfficialIrReleaseAdapter()
        self._adapter = source_adapter
        self._sealed_inner_adapter = source_adapter
        self._inner_adapter_type_token = (
            f"{type(source_adapter).__module__}."
            f"{type(source_adapter).__qualname__}"
        )
        self._seal_transport_identity = type(source_adapter) is OfficialIrReleaseAdapter
        self._sealed_inner_transport = getattr(source_adapter, "_fetch_bytes", None)
        self._inner_transport_mode = (
            "company_ir_default_https_v1"
            if (
                self._seal_transport_identity
                and self._sealed_inner_transport
                is OfficialIrReleaseAdapter._default_fetch_bytes
            )
            else (
                "company_ir_injected_transport_v1"
                if self._seal_transport_identity
                else "custom_ir_batch_adapter_v1"
            )
        )
        self._config_version = (
            "company_ir_config_v1_" + canonical_sha256(self._config_basis())[:16]
        )

    def poll(
        self,
        checkpoint: Any,
        *,
        observed_at_ms: Any,
        etag: Any = "",
        last_modified: Any = "",
        max_items: Any = 50,
    ) -> AdapterPollResult:
        self._assert_config_seal()
        clean_etag, clean_last_modified, safe_max_items = validate_poll_context(
            etag=etag,
            last_modified=last_modified,
            max_items=max_items,
        )
        if safe_max_items < self.max_candidates_per_poll:
            raise SourceMonitoringContractError(
                "COMPANY_IR_ITEM_CAPACITY_TOO_LOW",
                (
                    f"max_items={safe_max_items} is below the sealed company IR "
                    f"candidate bound {self.max_candidates_per_poll}"
                ),
            )
        captured_at_ms, observed_at = _native_observed_at(observed_at_ms)
        started_checkpoint, old_order, projections = _normalize_checkpoint(checkpoint)
        try:
            monitoring_batch = getattr(
                self._adapter,
                "monitoring_releases_batch",
                None,
            )
            if callable(monitoring_batch):
                payload = monitoring_batch(
                    list(self.symbols),
                    limit=self.per_symbol_limit,
                    force=self.force,
                )
            else:
                payload = self._adapter.recent_releases_batch(
                    list(self.symbols),
                    limit=self.per_symbol_limit,
                    force=self.force,
                )
        except Exception as exc:
            return AdapterPollResult.build(
                adapter_key=self.adapter_key,
                started_checkpoint=started_checkpoint,
                next_checkpoint=started_checkpoint,
                observed_items=(),
                source_errors=(SourcePollError.build(
                    "IR_POLL_ERROR",
                    str(exc)[:1_000] or "company IR poll failed",
                    "official_company_ir",
                ),),
                retry_after_ms=60_000,
                captured_at_ms=captured_at_ms,
                etag=clean_etag,
                last_modified=clean_last_modified,
            )

        if type(payload) is not dict:
            payload = {}
        errors = _source_errors(payload)
        rows = payload.get("rows") if type(payload.get("rows")) is list else []
        observed_items: list[dict[str, Any]] = []
        candidate_order: list[str] = []
        candidate_groups: dict[str, list[dict[str, Any]]] = {}
        duplicate_count = 0
        rejected_count = 0

        for row in rows:
            if type(row) is not dict:
                rejected_count += 1
                continue
            symbol = row.get("symbol")
            if type(symbol) is not str or symbol not in self.symbols:
                rejected_count += 1
                continue
            config = IR_FEEDS[symbol]
            hosts = set(config["hosts"])
            feed_url = _official_url(config["url"], allowed_hosts=hosts)
            publisher = config["publisher"]
            releases = row.get("releases") if type(row.get("releases")) is list else []
            for release in releases:
                if type(release) is not dict:
                    rejected_count += 1
                    continue
                title_value = release.get("title")
                summary_value = release.get("summary")
                title = title_value.strip()[:300] if type(title_value) is str else ""
                summary = summary_value.strip()[:800] if type(summary_value) is str else ""
                official_url = _official_url(
                    release.get("official_url"),
                    allowed_hosts=hosts,
                )
                published_at = _rfc3339(
                    release.get("published_at"),
                    observed_at=observed_at,
                )
                guid = _clean_guid(release.get("guid"))
                event_type = release.get("event_type")
                if (
                    not title
                    or not official_url
                    or not feed_url
                    or official_url == feed_url
                    or not published_at
                    or type(event_type) is not str
                    or event_type not in _IR_EVENT_TYPES
                ):
                    rejected_count += 1
                    continue
                (
                    identity_kind,
                    identity_value,
                    identity_sha,
                    projection_sha,
                ) = _identity_and_projection(
                    symbol=symbol,
                    guid=guid,
                    official_url=official_url,
                    title=title,
                    summary=summary,
                    published_at=published_at,
                )
                if identity_sha not in candidate_groups:
                    candidate_order.append(identity_sha)
                    candidate_groups[identity_sha] = []
                candidate_groups[identity_sha].append({
                    "symbol": symbol,
                    "publisher": publisher,
                    "feed_url": feed_url,
                    "release": {**release, "title": title, "summary": summary},
                    "official_url": official_url,
                    "published_at": published_at,
                    "guid": guid,
                    "identity_kind": identity_kind,
                    "identity_value": identity_value,
                    "identity_sha": identity_sha,
                    "projection_sha": projection_sha,
                })

        if len(candidate_groups) > MAX_IR_PROJECTIONS:
            errors = errors[: MAX_SOURCE_ERRORS_PER_POLL - 1]
            errors.append(SourcePollError.build(
                "COMPANY_IR_CHECKPOINT_CAPACITY_EXCEEDED",
                (
                    f"company IR poll returned {len(candidate_groups)} unique "
                    f"identities; the checkpoint capacity is {MAX_IR_PROJECTIONS}"
                ),
                "official_company_ir",
            ))
            return AdapterPollResult.build(
                adapter_key=self.adapter_key,
                started_checkpoint=started_checkpoint,
                next_checkpoint=started_checkpoint,
                observed_items=(),
                source_errors=errors,
                retry_after_ms=60_000,
                captured_at_ms=captured_at_ms,
                etag=clean_etag,
                last_modified=clean_last_modified,
                rejected_count=rejected_count + len(candidate_groups),
            )

        emitted_group_count: dict[str, int] = {}
        for identity_sha in candidate_order:
            group = candidate_groups[identity_sha]
            projection_values = {
                candidate["projection_sha"] for candidate in group
            }
            if len(projection_values) != 1:
                rejected_count += len(group)
                continue
            symbol = group[0]["symbol"]
            duplicate_count += len(group) - 1
            candidate = group[0]
            projection_sha = candidate["projection_sha"]
            previous_projection_sha = projections.get(identity_sha, "")
            if previous_projection_sha == projection_sha:
                duplicate_count += 1
                continue
            if emitted_group_count.get(symbol, 0) >= self.per_symbol_limit:
                continue
            if len(observed_items) >= safe_max_items:
                continue
            observed_items.append(_release_item(
                **candidate,
                previous_projection_sha=previous_projection_sha,
            ))
            emitted_group_count[symbol] = emitted_group_count.get(symbol, 0) + 1
            projections[identity_sha] = projection_sha

        next_order = [
            identity
            for identity in old_order
            if identity in candidate_groups and identity in projections
        ] + [
            identity
            for identity in candidate_order
            if identity in projections and identity not in old_order
        ]
        next_checkpoint = {
            "version": COMPANY_IR_CHECKPOINT_VERSION,
            "projections": [
                {
                    "identity_sha256": identity,
                    "rss_projection_sha256": projections[identity],
                }
                for identity in next_order
            ],
        }
        return AdapterPollResult.build(
            adapter_key=self.adapter_key,
            started_checkpoint=started_checkpoint,
            next_checkpoint=next_checkpoint,
            observed_items=observed_items,
            source_errors=errors,
            retry_after_ms=60_000 if errors else 0,
            captured_at_ms=captured_at_ms,
            etag=clean_etag,
            last_modified=clean_last_modified,
            duplicate_count=duplicate_count,
            rejected_count=rejected_count,
        )


CompanyIrAdapter = CompanyIrSourceAdapter

__all__ = [
    "COMPANY_IR_ADAPTER_KEY",
    "COMPANY_IR_CHECKPOINT_VERSION",
    "COMPANY_IR_CONFIG_BASIS_VERSION",
    "COMPANY_IR_IDENTITY_VERSION",
    "COMPANY_IR_POLL_INTERVAL_MS",
    "COMPANY_IR_RSS_PROJECTION_VERSION",
    "CompanyIrAdapter",
    "CompanyIrSourceAdapter",
    "MAX_IR_PROJECTIONS",
]
