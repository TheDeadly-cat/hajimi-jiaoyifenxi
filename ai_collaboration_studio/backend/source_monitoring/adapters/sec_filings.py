"""SEC submissions projection for the source-monitoring worker.

This module assumes only the local ``AdapterPollResult`` value contract: an
adapter exposes ``adapter_key`` and ``poll(checkpoint, observed_at_ms)``.  It
does not perform storage, provider, model, or trading work.  The wrapped EDGAR
adapter remains injectable so unit tests can be fixture-only.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from ...market.sec_edgar import (
    SEC_ARCHIVES_BASE,
    SEC_ALLOWED_FORMS,
    SEC_DEFAULT_FORMS,
    SEC_MONITOR_SYMBOLS,
    SEC_MONITORING_RECENT_SCOPE_VERSION,
    SecEdgarAdapter,
)
from ...source_poll_control import (
    SourcePollCancelled,
    SourcePollDeadlineExceeded,
    ensure_source_poll_active,
    validate_source_poll_control,
)
from ...source_inbox_contracts import PROJECT_SOURCE_ITEM_VERSION
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


SEC_FILINGS_ADAPTER_KEY = "sec_filings"
SEC_FILINGS_CHECKPOINT_VERSION = "sec_filings_checkpoint_v2"
SEC_FILINGS_CONFIG_BASIS_VERSION = "sec_filings_config_basis_v2"
SEC_FILINGS_PROJECTION_VERSION = "sec_v1"
SEC_FILINGS_DISCOVERY_TIME_SEMANTICS = "official_event_time_epoch_ms_v1"
SEC_FILINGS_POLL_INTERVAL_MS = 5 * 60 * 1_000
MAX_SEEN_ACCESSIONS = 1_000

_ACCESSION_RE = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}\Z")
_CIK_RE = re.compile(r"[0-9]{10}\Z")
_SYMBOL_RE = re.compile(r"US\.[A-Z][A-Z0-9.-]{0,14}\Z")
_PRIMARY_DOCUMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,239}\Z")


class _SecBatchAdapter(Protocol):
    """The deliberately small injected dependency used by this projection."""

    def recent_filings_batch(
        self,
        symbols: tuple[str, ...] | list[str],
        *,
        forms: tuple[str, ...] | list[str] | None = None,
        limit: int = 8,
        force: bool = False,
        deadline_monotonic_ms: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]: ...


def _checkpoint_error(message: str) -> SourceMonitoringContractError:
    return SourceMonitoringContractError(
        "SEC_FILINGS_CHECKPOINT_INVALID",
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


def _rfc3339_utc(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    base = utc.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f".{utc.microsecond:06d}".rstrip("0") if utc.microsecond else ""
    return f"{base}{fraction}Z"


def _event_time(
    accepted_at: Any,
    filing_date: Any,
    *,
    observed_at: datetime,
) -> str:
    raw = accepted_at.strip() if type(accepted_at) is str else ""
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return ""
        if parsed.tzinfo is None:
            return ""
        parsed = parsed.astimezone(timezone.utc)
    else:
        date_text = filing_date.strip() if type(filing_date) is str else ""
        try:
            parsed = datetime.strptime(date_text, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return ""
    if parsed > observed_at:
        return ""
    return _rfc3339_utc(parsed)


def _stable_event_time_ms(value: str) -> int:
    """Project a replay-stable millisecond anchor from the admitted SEC event."""

    if type(value) is not str:
        raise SourceMonitoringContractError(
            "SEC_FILINGS_EVENT_TIME_INVALID",
            "SEC event time must be a native RFC3339 string",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceMonitoringContractError(
            "SEC_FILINGS_EVENT_TIME_INVALID",
            "SEC event time is not valid RFC3339",
        ) from exc
    if parsed.tzinfo is None:
        raise SourceMonitoringContractError(
            "SEC_FILINGS_EVENT_TIME_INVALID",
            "SEC event time must include a UTC offset",
        )
    utc = parsed.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc - epoch
    milliseconds = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    if milliseconds < 0:
        raise SourceMonitoringContractError(
            "SEC_FILINGS_EVENT_TIME_INVALID",
            "SEC event time precedes the supported Unix epoch",
        )
    return milliseconds


def _normalize_checkpoint(value: Any) -> tuple[dict[str, Any], list[str]]:
    checkpoint = normalize_checkpoint(value)
    if checkpoint == {}:
        return checkpoint, []
    if checkpoint.get("version") == "sec_filings_checkpoint_v1":
        raise SourceMonitoringContractError(
            "SEC_BASELINE_UPGRADE_REQUIRED",
            "Legacy SEC checkpoint cannot prove a complete seed baseline; disable the adapter and explicitly migrate its config with an empty replacement checkpoint to establish a new bounded baseline. Existing inbox items and materials must be retained.",
        )
    if set(checkpoint) != {"version", "seen_accessions"}:
        raise _checkpoint_error("SEC checkpoint fields do not match v2")
    if checkpoint.get("version") != SEC_FILINGS_CHECKPOINT_VERSION:
        raise _checkpoint_error("SEC checkpoint version is unsupported")
    raw_seen = checkpoint.get("seen_accessions")
    if type(raw_seen) is not list or len(raw_seen) > MAX_SEEN_ACCESSIONS:
        raise _checkpoint_error("seen_accessions must be a bounded native list")
    seen: list[str] = []
    for accession in raw_seen:
        if type(accession) is not str or not _ACCESSION_RE.fullmatch(accession):
            raise _checkpoint_error("seen_accessions contains an invalid accession")
        if accession in seen:
            raise _checkpoint_error("seen_accessions contains a duplicate accession")
        seen.append(accession)
    return checkpoint, seen


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
            code = "SEC_SOURCE_ERROR"
        if type(message) is not str or not message.strip():
            message = "SEC source returned an unspecified error"
        scope_value = raw.get("symbol") or raw.get("source") or "sec_edgar"
        scope = scope_value if type(scope_value) is str else "sec_edgar"
        errors.append(SourcePollError.build(code, message[:1_000], scope[:160]))
    return errors


def _official_sec_url(
    value: Any,
    *,
    cik: str,
    accession: str,
    primary_document: str,
) -> str:
    if (
        type(value) is not str
        or not _CIK_RE.fullmatch(cik)
        or not _ACCESSION_RE.fullmatch(accession)
        or not _PRIMARY_DOCUMENT_RE.fullmatch(primary_document)
        or primary_document in {".", ".."}
    ):
        return ""
    clean = value.strip()
    try:
        parsed = urlsplit(clean)
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.sec.gov"
        or not parsed.path.startswith("/Archives/edgar/data/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return ""
    expected = (
        f"{SEC_ARCHIVES_BASE}/{int(cik)}/"
        f"{accession.replace('-', '')}/{primary_document}"
    )
    return clean if clean == expected else ""


def _filing_item(
    *,
    symbol: str,
    cik: str,
    company_name: str,
    filing: dict[str, Any],
    event_time: str,
    official_url: str,
) -> dict[str, Any]:
    accession = filing["accession_number"]
    form = filing["form"]
    description = filing.get("description")
    description_text = description.strip() if type(description) is str else ""
    raw_items = filing.get("items")
    form_items = []
    if type(raw_items) is str:
        form_items = [part.strip() for part in raw_items.split(",") if part.strip()][:40]
    primary_document = filing.get("primary_document")
    primary_document_text = (
        primary_document.strip()[:240] if type(primary_document) is str else ""
    )
    raw_accepted_at = filing.get("accepted_at")
    has_accepted_at = (
        type(raw_accepted_at) is str and bool(raw_accepted_at.strip())
    )
    headline = f"{symbol} filed SEC {form} ({accession})"
    summary = description_text or f"Official SEC {form} filing metadata for {symbol}."
    return {
        "version": PROJECT_SOURCE_ITEM_VERSION,
        "external_item_id": accession,
        "item_type": "sec_filing",
        "severity": "info",
        "occurred_at": event_time,
        "published_at": event_time,
        "entities": [
            {"kind": "security", "id": symbol, "label": symbol.removeprefix("US.")},
            {"kind": "issuer", "id": cik, "label": company_name or symbol},
        ],
        "headline": headline,
        "summary": summary[:8_000],
        "facts": [{
            "claim": f"The SEC submissions feed records accession {accession} as form {form}.",
            "source_indexes": [0],
        }],
        "sources": [{
            "url": official_url,
            "publisher": "U.S. Securities and Exchange Commission EDGAR",
            "source_type": "regulatory_filing",
            "published_at": event_time,
            "content_sha256": "",
        }],
        "impact_hypotheses": [],
        "unknowns": [
            "The filing body was not fetched or hashed by this submissions-metadata poll."
        ],
        "confidence": 1.0,
        "recommended_route": "notify_only",
        "extensions": {
            "sec_v1": {
                "accession_number": accession,
                "accepted_at": event_time if has_accepted_at else "",
                "cik": cik,
                # The legacy field name is retained for schema compatibility,
                # but the value is the stable official-event anchor.  Local
                # first discovery remains Source Inbox ``received_at``.
                "discovered_at_ms": _stable_event_time_ms(event_time),
                "filing_date": filing.get("filing_date") or "",
                "form": form,
                "items": form_items,
                "primary_document": primary_document_text,
                "submissions_metadata_only": True,
                "symbol": symbol,
            }
        },
    }


class SecFilingsSourceAdapter:
    """Project accepted SEC submission metadata into stable inbox items."""

    contract_version = SOURCE_ADAPTER_CONTRACT_VERSION
    adapter_key = SEC_FILINGS_ADAPTER_KEY
    official_source = True
    execution_capability = "none"
    live_trading_allowed = False

    @property
    def allowed_symbols(self) -> tuple[str, ...]:
        return self._allowed_symbols

    @property
    def allowed_forms(self) -> tuple[str, ...]:
        return self._allowed_forms

    @property
    def per_symbol_limit(self) -> int:
        return self._per_symbol_limit

    @property
    def max_candidates_per_poll(self) -> int:
        return len(self.allowed_symbols) * self.per_symbol_limit

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
        return {
            "version": SEC_FILINGS_CONFIG_BASIS_VERSION,
            "adapter_key": self.adapter_key,
            "checkpoint_version": SEC_FILINGS_CHECKPOINT_VERSION,
            "projection_version": SEC_FILINGS_PROJECTION_VERSION,
            "discovery_time_semantics": SEC_FILINGS_DISCOVERY_TIME_SEMANTICS,
            "allowed_symbols": list(self.allowed_symbols),
            "allowed_forms": list(self.allowed_forms),
            "per_symbol_limit": self.per_symbol_limit,
            "max_candidates_per_poll": self.max_candidates_per_poll,
            "inner_adapter_type": self._inner_adapter_type_token,
            "inner_transport_mode": self._inner_transport_mode,
            "inner_user_agent_sha256": self._inner_user_agent_sha256,
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
                "SEC_FILINGS_SOURCE_PROVENANCE_DRIFT",
                "SEC filings inner adapter changed after construction",
            )
        if self._seal_transport_identity and (
            getattr(self._adapter, "_fetch_json", None)
            is not self._sealed_inner_transport
        ):
            raise SourceMonitoringContractError(
                "SEC_FILINGS_SOURCE_PROVENANCE_DRIFT",
                "SEC filings transport changed after construction",
            )
        if self._seal_transport_identity and (
            getattr(self._adapter, "user_agent", None)
            != self._sealed_inner_user_agent
        ):
            raise SourceMonitoringContractError(
                "SEC_FILINGS_SOURCE_PROVENANCE_DRIFT",
                "SEC filings user agent changed after construction",
            )
        expected = "sec_filings_config_v2_" + canonical_sha256(
            self._config_basis()
        )[:16]
        if self.config_version != expected:
            raise SourceMonitoringContractError(
                "SEC_FILINGS_CONFIG_DRIFT",
                "SEC filings adapter configuration changed after construction",
            )

    def __init__(
        self,
        *,
        adapter: _SecBatchAdapter | None = None,
        allowed_symbols: tuple[str, ...] | list[str] = SEC_MONITOR_SYMBOLS,
        allowed_forms: tuple[str, ...] | list[str] = SEC_DEFAULT_FORMS,
        per_symbol_limit: int = 6,
        force: bool = False,
        poll_interval_ms: int = SEC_FILINGS_POLL_INTERVAL_MS,
    ) -> None:
        if type(allowed_symbols) not in {list, tuple}:
            raise ValueError("allowed_symbols must be a native list or tuple")
        symbols: list[str] = []
        for raw_symbol in allowed_symbols:
            if (
                type(raw_symbol) is not str
                or not _SYMBOL_RE.fullmatch(raw_symbol)
                or raw_symbol in symbols
            ):
                raise ValueError(
                    "allowed_symbols must contain unique canonical US symbols"
                )
            symbols.append(raw_symbol)
        if not symbols:
            raise ValueError("allowed_symbols must contain only canonical US symbols")

        if type(allowed_forms) not in {list, tuple}:
            raise ValueError("allowed_forms must be a native list or tuple")
        forms: list[str] = []
        for raw_form in allowed_forms:
            if (
                type(raw_form) is not str
                or raw_form not in SEC_ALLOWED_FORMS
                or raw_form in forms
            ):
                raise ValueError(
                    "allowed_forms must be unique canonical SEC form names"
                )
            forms.append(raw_form)
        if not forms:
            raise ValueError("allowed_forms must remain inside SEC_ALLOWED_FORMS")
        if type(per_symbol_limit) is not int or not 1 <= per_symbol_limit <= 40:
            raise ValueError("per_symbol_limit must be a native integer from 1 to 40")
        if len(symbols) * per_symbol_limit > MAX_OBSERVED_ITEMS_PER_POLL:
            raise ValueError(
                "SEC candidate bound must remain at or below 50 items per poll"
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
            getattr(adapter, "recent_filings_batch", None)
        ):
            raise ValueError("adapter must implement recent_filings_batch")

        self._allowed_symbols = tuple(symbols)
        self._allowed_forms = tuple(forms)
        self._per_symbol_limit = per_symbol_limit
        self._force = force
        self._poll_interval_ms = poll_interval_ms
        source_adapter = adapter or SecEdgarAdapter(
            allowed_symbols=self.allowed_symbols
        )
        self._adapter = source_adapter
        self._sealed_inner_adapter = source_adapter
        self._inner_adapter_type_token = (
            f"{type(source_adapter).__module__}."
            f"{type(source_adapter).__qualname__}"
        )
        self._seal_transport_identity = type(source_adapter) is SecEdgarAdapter
        self._sealed_inner_transport = getattr(source_adapter, "_fetch_json", None)
        self._sealed_inner_user_agent = getattr(source_adapter, "user_agent", None)
        self._inner_transport_mode = (
            "sec_default_https_v1"
            if (
                self._seal_transport_identity
                and self._sealed_inner_transport
                is SecEdgarAdapter._default_fetch_json
            )
            else (
                "sec_injected_transport_v1"
                if self._seal_transport_identity
                else "custom_sec_batch_adapter_v1"
            )
        )
        self._inner_user_agent_sha256 = (
            canonical_sha256({"user_agent": self._sealed_inner_user_agent})
            if self._seal_transport_identity
            else ""
        )
        self._config_version = (
            "sec_filings_config_v2_" + canonical_sha256(self._config_basis())[:16]
        )

    def poll(
        self,
        checkpoint: Any,
        *,
        observed_at_ms: Any,
        deadline_monotonic_ms: Any = 0,
        cancel_event: threading.Event | None = None,
        etag: Any = "",
        last_modified: Any = "",
        max_items: Any = 50,
    ) -> AdapterPollResult:
        return self._poll(
            checkpoint,
            observed_at_ms=observed_at_ms,
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
            etag=etag,
            last_modified=last_modified,
            max_items=max_items,
            seed_baseline=False,
        )

    def poll_seed_baseline(
        self,
        checkpoint: Any,
        *,
        observed_at_ms: Any,
        deadline_monotonic_ms: Any = 0,
        cancel_event: threading.Event | None = None,
        etag: Any = "",
        last_modified: Any = "",
        max_items: Any = 50,
    ) -> AdapterPollResult:
        """Seed all IDs in one complete bounded recent snapshot, not one delivery batch."""

        if normalize_checkpoint(checkpoint) != {}:
            raise _checkpoint_error("SEC initial baseline requires an explicitly empty checkpoint")
        return self._poll(
            checkpoint,
            observed_at_ms=observed_at_ms,
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
            etag=etag,
            last_modified=last_modified,
            max_items=max_items,
            seed_baseline=True,
        )

    def _poll(
        self,
        checkpoint: Any,
        *,
        observed_at_ms: Any,
        deadline_monotonic_ms: Any,
        cancel_event: threading.Event | None,
        etag: Any,
        last_modified: Any,
        max_items: Any,
        seed_baseline: bool,
    ) -> AdapterPollResult:
        self._assert_config_seal()
        deadline, event = validate_source_poll_control(
            deadline_monotonic_ms=deadline_monotonic_ms,
            cancel_event=cancel_event,
        )
        clean_etag, clean_last_modified, safe_max_items = validate_poll_context(
            etag=etag,
            last_modified=last_modified,
            max_items=max_items,
        )
        if safe_max_items < self.max_candidates_per_poll:
            raise SourceMonitoringContractError(
                "SEC_FILINGS_ITEM_CAPACITY_TOO_LOW",
                (
                    f"max_items={safe_max_items} is below the sealed SEC candidate "
                    f"bound {self.max_candidates_per_poll}"
                ),
            )
        captured_at_ms, observed_at = _native_observed_at(observed_at_ms)
        started_checkpoint, seen_order = _normalize_checkpoint(checkpoint)
        seen = set(seen_order)
        try:
            ensure_source_poll_active(
                deadline_monotonic_ms=deadline,
                cancel_event=event,
            )
            monitoring_batch = getattr(
                self._adapter,
                "monitoring_filings_batch",
                None,
            )
            if callable(monitoring_batch):
                if self._seal_transport_identity:
                    payload = monitoring_batch(
                        list(self.allowed_symbols),
                        forms=list(self.allowed_forms),
                        limit=self.per_symbol_limit,
                        force=self.force,
                        deadline_monotonic_ms=deadline,
                        cancel_event=event,
                    )
                else:
                    payload = monitoring_batch(
                        list(self.allowed_symbols),
                        forms=list(self.allowed_forms),
                        limit=self.per_symbol_limit,
                        force=self.force,
                    )
            else:
                if self._seal_transport_identity:
                    payload = self._adapter.recent_filings_batch(
                        list(self.allowed_symbols),
                        forms=list(self.allowed_forms),
                        limit=self.per_symbol_limit,
                        force=self.force,
                        deadline_monotonic_ms=deadline,
                        cancel_event=event,
                    )
                else:
                    payload = self._adapter.recent_filings_batch(
                        list(self.allowed_symbols),
                        forms=list(self.allowed_forms),
                        limit=self.per_symbol_limit,
                        force=self.force,
                    )
            ensure_source_poll_active(
                deadline_monotonic_ms=deadline,
                cancel_event=event,
            )
        except (SourcePollCancelled, SourcePollDeadlineExceeded):
            raise
        except Exception as exc:
            return AdapterPollResult.build(
                adapter_key=self.adapter_key,
                started_checkpoint=started_checkpoint,
                next_checkpoint=started_checkpoint,
                observed_items=(),
                source_errors=(SourcePollError.build(
                    "SEC_POLL_ERROR",
                    str(exc)[:1_000] or "SEC poll failed",
                    "sec_edgar",
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
        complete_recent_scope = (
            payload.get("monitoring_recent_scope_version") == SEC_MONITORING_RECENT_SCOPE_VERSION
            and payload.get("monitoring_recent_scope_complete") is True
            and len(rows) == len(self.allowed_symbols)
            and all(type(row) is dict for row in rows)
            and [row.get("symbol") for row in rows] == list(self.allowed_symbols)
        )
        observed_items: list[dict[str, Any]] = []
        new_accessions: list[str] = []
        candidate_order: list[str] = []
        candidate_groups: dict[str, list[dict[str, Any]]] = {}
        duplicate_count = 0
        rejected_count = 0

        for row in rows:
            if type(row) is not dict:
                rejected_count += 1
                continue
            symbol = row.get("symbol")
            cik = row.get("cik")
            if (
                type(symbol) is not str
                or symbol not in self.allowed_symbols
                or type(cik) is not str
                or not _CIK_RE.fullmatch(cik)
            ):
                rejected_count += 1
                continue
            company = row.get("company_name")
            company_name = company.strip()[:240] if type(company) is str else symbol
            filings = row.get("filings") if type(row.get("filings")) is list else []
            for filing in filings:
                if type(filing) is not dict:
                    rejected_count += 1
                    continue
                accession = filing.get("accession_number")
                form = filing.get("form")
                accepted_at = filing.get("accepted_at")
                filing_date = filing.get("filing_date")
                if (
                    type(accession) is not str
                    or not _ACCESSION_RE.fullmatch(accession)
                    or type(form) is not str
                    or form not in self.allowed_forms
                    or type(accepted_at) is not str
                    or type(filing_date) is not str
                ):
                    rejected_count += 1
                    continue
                event_time = _event_time(
                    accepted_at,
                    filing_date,
                    observed_at=observed_at,
                )
                primary_document = filing.get("primary_document")
                official_url = _official_sec_url(
                    filing.get("official_url"),
                    cik=cik,
                    accession=accession,
                    primary_document=(
                        primary_document if type(primary_document) is str else ""
                    ),
                )
                if not event_time or not official_url:
                    rejected_count += 1
                    continue
                item = _filing_item(
                    symbol=symbol,
                    cik=cik,
                    company_name=company_name,
                    filing=filing,
                    event_time=event_time,
                    official_url=official_url,
                )
                if accession not in candidate_groups:
                    candidate_order.append(accession)
                    candidate_groups[accession] = []
                candidate_groups[accession].append({
                    "symbol": symbol,
                    "item": item,
                    "projection_sha256": canonical_sha256(item),
                })

        scope_and_seen_count = len(set(candidate_groups) | seen)
        if scope_and_seen_count > MAX_SEEN_ACCESSIONS:
            errors = errors[: MAX_SOURCE_ERRORS_PER_POLL - 1]
            errors.append(SourcePollError.build(
                "SEC_CHECKPOINT_CAPACITY_EXCEEDED",
                (
                    f"SEC recent scope and retained identities require {scope_and_seen_count} unique accessions; "
                    f"the checkpoint capacity is {MAX_SEEN_ACCESSIONS}; committed identities are never evicted"
                ),
                "sec_edgar",
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

        emitted_per_symbol: dict[str, int] = {}
        for accession in candidate_order:
            group = candidate_groups[accession]
            projection_values = {
                candidate["projection_sha256"] for candidate in group
            }
            if len(projection_values) != 1:
                rejected_count += len(group)
                continue
            duplicate_count += len(group) - 1
            if accession in seen:
                duplicate_count += 1
                continue
            symbol = group[0]["symbol"]
            if emitted_per_symbol.get(symbol, 0) >= self.per_symbol_limit:
                continue
            if len(observed_items) >= safe_max_items:
                continue
            observed_items.append(group[0]["item"])
            emitted_per_symbol[symbol] = emitted_per_symbol.get(symbol, 0) + 1
            seen.add(accession)
            new_accessions.append(accession)

        if seed_baseline and (not complete_recent_scope or errors or rejected_count):
            errors = errors[: MAX_SOURCE_ERRORS_PER_POLL - 1]
            errors.append(SourcePollError.build(
                "SEC_BASELINE_SCOPE_INCOMPLETE",
                "SEC seed requires every selected-form ID in the complete bounded recent scope; incomplete, malformed or filtered source evidence cannot establish a baseline",
                "sec_edgar",
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
                rejected_count=rejected_count,
            )

        next_seen = candidate_order if seed_baseline else new_accessions + [
            accession
            for accession in seen_order
            if accession not in new_accessions
        ]
        next_checkpoint = {
            "version": SEC_FILINGS_CHECKPOINT_VERSION,
            "seen_accessions": next_seen,
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


SecFilingsAdapter = SecFilingsSourceAdapter

__all__ = [
    "MAX_SEEN_ACCESSIONS",
    "SEC_FILINGS_ADAPTER_KEY",
    "SEC_FILINGS_CHECKPOINT_VERSION",
    "SEC_FILINGS_CONFIG_BASIS_VERSION",
    "SEC_FILINGS_DISCOVERY_TIME_SEMANTICS",
    "SEC_FILINGS_POLL_INTERVAL_MS",
    "SEC_FILINGS_PROJECTION_VERSION",
    "SecFilingsAdapter",
    "SecFilingsSourceAdapter",
]
