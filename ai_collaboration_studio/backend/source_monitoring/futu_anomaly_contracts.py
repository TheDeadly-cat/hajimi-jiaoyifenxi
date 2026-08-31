"""Pure, fail-closed contracts for deterministic Futu anomaly signals.

This module deliberately performs no Futu, network, storage, provider, model,
account, order, or execution action.  It admits one complete four-symbol quote
snapshot, applies sealed v1 hysteresis rules, and projects neutral Source Inbox
items.  Snapshot polling and at-least-once persistence remain adapter/supervisor
responsibilities.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from ..market.futu_readonly import (
    LIVE_QUOTE_MAX_AGE_SECONDS,
    STORAGE_SYMBOLS,
    validate_storage_quote_snapshot,
)
from ..source_inbox_contracts import PROJECT_SOURCE_ITEM_VERSION
from .contracts import (
    SourceMonitoringContractError,
    SourcePollError,
    canonical_json,
    canonical_sha256,
    normalize_checkpoint,
)


FUTU_ANOMALY_CHECKPOINT_VERSION = "futu_anomaly_checkpoint_v1"
FUTU_ANOMALY_PROJECTION_VERSION = "futu_anomaly_projection_v1"
FUTU_ANOMALY_IDENTITY_VERSION = "futu_anomaly_identity_v1"
FUTU_ANOMALY_POLICY_VERSION = "futu_anomaly_policy_v1"
FUTU_ANOMALY_SOURCE_URL = (
    "https://openapi.futunn.com/futu-api-doc/en/quote/"
    "get-market-snapshot.html"
)
MAX_FUTU_ANOMALY_SNAPSHOT_BYTES = 256 * 1024

PRICE_UP_RULE_ID = "price_up_5pct"
PRICE_DOWN_RULE_ID = "price_down_5pct"
AMPLITUDE_RULE_ID = "amplitude_8pct"
VOLUME_RATIO_RULE_ID = "volume_ratio_3x"
FUTU_ANOMALY_RULE_IDS = (
    PRICE_UP_RULE_ID,
    PRICE_DOWN_RULE_ID,
    AMPLITUDE_RULE_ID,
    VOLUME_RATIO_RULE_ID,
)

_RULE_ORDER = {rule_id: index for index, rule_id in enumerate(FUTU_ANOMALY_RULE_IDS)}
_RULE_SPECS: dict[str, dict[str, str]] = {
    PRICE_UP_RULE_ID: {
        "metric": "change_rate",
        "entry_threshold": "5",
        "exit_threshold": "4",
        "comparison": "positive",
        "signal_direction": "up_observation",
        "label": "price increase",
        "unit": "percent",
    },
    PRICE_DOWN_RULE_ID: {
        "metric": "change_rate",
        "entry_threshold": "-5",
        "exit_threshold": "-4",
        "comparison": "negative",
        "signal_direction": "down_observation",
        "label": "price decrease",
        "unit": "percent",
    },
    AMPLITUDE_RULE_ID: {
        "metric": "amplitude",
        "entry_threshold": "8",
        "exit_threshold": "6",
        "comparison": "positive",
        "signal_direction": "none",
        "label": "intraday amplitude",
        "unit": "percent",
    },
    VOLUME_RATIO_RULE_ID: {
        "metric": "volume_ratio",
        "entry_threshold": "3",
        "exit_threshold": "2.5",
        "comparison": "positive",
        "signal_direction": "none",
        "label": "volume ratio",
        "unit": "ratio",
    },
}

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_FIELD_WORD_RE = re.compile(r"[a-z0-9]+")
_US_EASTERN = ZoneInfo("America/New_York")
_SCOPE = "futu_anomaly"

_FORBIDDEN_SNAPSHOT_FIELD_WORDS = frozenset({
    "account",
    "accounts",
    "auth",
    "authorization",
    "balance",
    "balances",
    "brokerage",
    "cash",
    "command",
    "commands",
    "credential",
    "credentials",
    "cookie",
    "execute",
    "funding",
    "key",
    "keys",
    "order",
    "orders",
    "password",
    "position",
    "positions",
    "secret",
    "signature",
    "session",
    "token",
    "trade",
    "trades",
    "trading",
    "transfer",
    "transfers",
    "wallet",
    "wallets",
    "withdraw",
    "withdrawals",
})
_FORBIDDEN_SNAPSHOT_COMPACT_FIELDS = frozenset({
    "accesstoken",
    "accountid",
    "apikey",
    "authtoken",
    "clientsecret",
    "orderid",
    "privatekey",
    "refreshtoken",
    "sessionid",
    "signingkey",
    "tradeid",
})
_ROOT_SAFETY_FIELDS = frozenset({"execution_capability", "live_trading_allowed"})

_UNKNOWN_CAUSE = "The cause of this market anomaly is unknown."
_UNKNOWN_NEWS = "No news attribution or causal attribution was performed."
_UNKNOWN_IMPLICATION = (
    "No directional forecast or trading implication is inferred from this signal."
)
_UNKNOWNS = [_UNKNOWN_CAUSE, _UNKNOWN_NEWS, _UNKNOWN_IMPLICATION]


def futu_anomaly_policy_manifest() -> dict[str, Any]:
    """Return a defensive copy of the complete sealed v1 policy basis."""

    manifest = {
        "policy_version": FUTU_ANOMALY_POLICY_VERSION,
        "checkpoint_version": FUTU_ANOMALY_CHECKPOINT_VERSION,
        "identity_version": FUTU_ANOMALY_IDENTITY_VERSION,
        "projection_version": FUTU_ANOMALY_PROJECTION_VERSION,
        "source_url": FUTU_ANOMALY_SOURCE_URL,
        "source_content_hash_semantics": (
            "stable_session_rule_signal_semantics_not_web_body"
        ),
        "storage_symbols": list(STORAGE_SYMBOLS),
        "snapshot_max_canonical_json_bytes": MAX_FUTU_ANOMALY_SNAPSHOT_BYTES,
        "episode_policy": {
            "identity_basis": [
                "symbol",
                "us_eastern_market_date",
                "rule_id",
            ],
            "occurred_at_anchor": "09:30:00 America/New_York",
            "emission": "once_per_us_eastern_market_date_per_rule",
            "exact_replay_duplicate_basis": "last_emitted_rule_ids",
            "news_attribution_performed": False,
            "causal_attribution": "none",
            "signal_only": True,
        },
        "required_quote_state": {
            "quote_is_live": True,
            "freshness_basis": "live_20m_window",
            "research_ready": True,
            "security_status": "NORMAL",
            "suspended": False,
        },
        "rules": [
            {
                "rule_id": rule_id,
                "metric": _RULE_SPECS[rule_id]["metric"],
                "entry_threshold": _RULE_SPECS[rule_id]["entry_threshold"],
                "exit_threshold": _RULE_SPECS[rule_id]["exit_threshold"],
                "comparison": _RULE_SPECS[rule_id]["comparison"],
                "direction": _RULE_SPECS[rule_id]["signal_direction"],
                "unit": _RULE_SPECS[rule_id]["unit"],
            }
            for rule_id in FUTU_ANOMALY_RULE_IDS
        ],
    }
    return copy.deepcopy(manifest)


def _contract_error(code: str, message: str) -> SourceMonitoringContractError:
    return SourceMonitoringContractError(code, message)


def _utc_iso(moment: datetime) -> str:
    utc_moment = moment.astimezone(timezone.utc)
    timespec = (
        "milliseconds"
        if utc_moment.microsecond % 1_000 == 0
        else "microseconds"
    )
    return (
        utc_moment
        .isoformat(timespec=timespec)
        .replace("+00:00", "Z")
    )


def _session_anchor(session_date: str) -> str:
    """Return the deterministic 09:30 America/New_York episode anchor."""

    market_date = date.fromisoformat(session_date)
    local_open = datetime.combine(market_date, time(hour=9, minute=30), _US_EASTERN)
    return _utc_iso(local_open)


def _parse_rfc3339(value: Any, *, field: str) -> tuple[str, datetime]:
    if type(value) is not str or not _RFC3339_RE.fullmatch(value):
        raise _contract_error(
            "FUTU_ANOMALY_TIME_INVALID",
            f"{field} must be an explicit RFC3339 timestamp",
        )
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise _contract_error(
            "FUTU_ANOMALY_TIME_INVALID",
            f"{field} is not a valid RFC3339 timestamp",
        ) from exc
    if parsed.tzinfo is None:
        raise _contract_error(
            "FUTU_ANOMALY_TIME_INVALID",
            f"{field} must include a timezone",
        )
    parsed = parsed.astimezone(timezone.utc)
    return _utc_iso(parsed), parsed


def _decimal_string(value: Any, *, field: str, positive: bool = False) -> str:
    if type(value) not in {int, float} or type(value) is bool:
        raise _contract_error(
            "FUTU_ANOMALY_METRIC_INVALID",
            f"{field} must be a native finite JSON number",
        )
    if type(value) is float and not math.isfinite(value):
        raise _contract_error(
            "FUTU_ANOMALY_METRIC_INVALID",
            f"{field} must be finite",
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise _contract_error(
            "FUTU_ANOMALY_METRIC_INVALID",
            f"{field} is not a valid decimal number",
        ) from exc
    if not number.is_finite() or (positive and number <= 0):
        raise _contract_error(
            "FUTU_ANOMALY_METRIC_INVALID",
            f"{field} is outside its admitted numeric domain",
        )
    if abs(number) > Decimal("1000000000000000"):
        raise _contract_error(
            "FUTU_ANOMALY_METRIC_INVALID",
            f"{field} exceeds the sealed decimal bound",
        )
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0"}:
        rendered = "0"
    if len(rendered) > 80:
        raise _contract_error(
            "FUTU_ANOMALY_METRIC_INVALID",
            f"{field} exceeds the canonical string bound",
        )
    return rendered


def _decimal(value: str) -> Decimal:
    return Decimal(value)


def _sorted_rule_ids(value: set[str] | list[str] | tuple[str, ...]) -> list[str]:
    return sorted(value, key=lambda rule_id: _RULE_ORDER[rule_id])


def _reject_unsafe_snapshot_fields(value: Any, *, path: str = "$", root: bool = True) -> None:
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _contract_error(
                    "FUTU_ANOMALY_SNAPSHOT_UNSAFE",
                    f"{path} contains a non-native string key",
                )
            separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
            words = frozenset(_FIELD_WORD_RE.findall(separated.casefold()))
            compact = re.sub(r"[^a-z0-9]", "", key.casefold())
            explicitly_safe_root_field = root and key in _ROOT_SAFETY_FIELDS
            if (
                words & _FORBIDDEN_SNAPSHOT_FIELD_WORDS
                or compact in _FORBIDDEN_SNAPSHOT_COMPACT_FIELDS
            ) and not explicitly_safe_root_field:
                raise _contract_error(
                    "FUTU_ANOMALY_SNAPSHOT_UNSAFE",
                    f"{path}.{key} is outside the read-only quote boundary",
                )
            _reject_unsafe_snapshot_fields(item, path=f"{path}.{key}", root=False)
    elif type(value) is list:
        for index, item in enumerate(value):
            _reject_unsafe_snapshot_fields(item, path=f"{path}[{index}]", root=False)


def _normalize_rule_list(value: Any, *, field: str) -> list[str]:
    if type(value) is not list:
        raise _contract_error(
            "FUTU_ANOMALY_CHECKPOINT_INVALID",
            f"{field} must be a list",
        )
    if any(type(rule_id) is not str for rule_id in value):
        raise _contract_error(
            "FUTU_ANOMALY_CHECKPOINT_INVALID",
            f"{field} must contain native strings",
        )
    if len(value) != len(set(value)) or any(
        rule_id not in _RULE_ORDER for rule_id in value
    ):
        raise _contract_error(
            "FUTU_ANOMALY_CHECKPOINT_INVALID",
            f"{field} contains a duplicate or unknown rule",
        )
    expected = _sorted_rule_ids(value)
    if value != expected:
        raise _contract_error(
            "FUTU_ANOMALY_CHECKPOINT_INVALID",
            f"{field} is not in canonical rule order",
        )
    return list(value)


def normalize_futu_anomaly_checkpoint(
    value: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate the exact system-owned v1 checkpoint or admit ``{}`` initially."""

    checkpoint = normalize_checkpoint(value)
    if checkpoint == {}:
        return checkpoint, {}
    if set(checkpoint) != {"version", "symbols"}:
        raise _contract_error(
            "FUTU_ANOMALY_CHECKPOINT_INVALID",
            "checkpoint fields do not match futu_anomaly_checkpoint_v1",
        )
    if checkpoint.get("version") != FUTU_ANOMALY_CHECKPOINT_VERSION:
        raise _contract_error(
            "FUTU_ANOMALY_CHECKPOINT_INVALID",
            "checkpoint version is unsupported",
        )
    entries = checkpoint.get("symbols")
    if type(entries) is not list or len(entries) != len(STORAGE_SYMBOLS):
        raise _contract_error(
            "FUTU_ANOMALY_CHECKPOINT_INVALID",
            "checkpoint must contain exactly the sealed storage universe",
        )
    expected_order = sorted(STORAGE_SYMBOLS)
    if [entry.get("symbol") if type(entry) is dict else None for entry in entries] != expected_order:
        raise _contract_error(
            "FUTU_ANOMALY_CHECKPOINT_INVALID",
            "checkpoint symbols are incomplete, duplicated, or out of order",
        )

    states: dict[str, dict[str, Any]] = {}
    expected_fields = {
        "symbol",
        "session_date",
        "active_rule_ids",
        "emitted_rule_ids",
        "last_observed_at",
        "last_observation_sha256",
        "last_emitted_observation_sha256",
        "last_emitted_rule_ids",
    }
    for entry in entries:
        if type(entry) is not dict or set(entry) != expected_fields:
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "checkpoint symbol entry fields are invalid",
            )
        symbol = entry.get("symbol")
        if type(symbol) is not str or symbol not in STORAGE_SYMBOLS or symbol in states:
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "checkpoint symbol is outside the sealed storage universe",
            )
        session_date = entry.get("session_date")
        if type(session_date) is not str or not _DATE_RE.fullmatch(session_date):
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "checkpoint session_date is invalid",
            )
        try:
            datetime.strptime(session_date, "%Y-%m-%d")
        except ValueError as exc:
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "checkpoint session_date is not a calendar date",
            ) from exc
        last_observed_at, last_observed = _parse_rfc3339(
            entry.get("last_observed_at"),
            field="checkpoint.last_observed_at",
        )
        if entry.get("last_observed_at") != last_observed_at:
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "checkpoint last_observed_at is not canonical UTC",
            )
        if last_observed.astimezone(_US_EASTERN).date().isoformat() != session_date:
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "checkpoint session_date does not match last_observed_at",
            )
        observation_sha = entry.get("last_observation_sha256")
        emitted_sha = entry.get("last_emitted_observation_sha256")
        if type(observation_sha) is not str or not _SHA256_RE.fullmatch(observation_sha):
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "checkpoint last_observation_sha256 is invalid",
            )
        if type(emitted_sha) is not str or (
            emitted_sha != "" and not _SHA256_RE.fullmatch(emitted_sha)
        ):
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "checkpoint last_emitted_observation_sha256 is invalid",
            )
        active = _normalize_rule_list(
            entry.get("active_rule_ids"),
            field="checkpoint.active_rule_ids",
        )
        emitted = _normalize_rule_list(
            entry.get("emitted_rule_ids"),
            field="checkpoint.emitted_rule_ids",
        )
        last_emitted_rules = _normalize_rule_list(
            entry.get("last_emitted_rule_ids"),
            field="checkpoint.last_emitted_rule_ids",
        )
        mutually_exclusive_price_rules = {PRICE_UP_RULE_ID, PRICE_DOWN_RULE_ID}
        if mutually_exclusive_price_rules.issubset(active):
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "checkpoint cannot mark price-up and price-down active together",
            )
        if mutually_exclusive_price_rules.issubset(last_emitted_rules):
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "one observation cannot emit price-up and price-down together",
            )
        if not set(active).issubset(emitted):
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "checkpoint active rules must already have been emitted",
            )
        if not set(last_emitted_rules).issubset(emitted):
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "checkpoint last emitted rules must be in emitted_rule_ids",
            )
        if (
            emitted_sha == observation_sha
            and not set(last_emitted_rules).issubset(active)
        ):
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "rules emitted by the last observation must be active there",
            )
        if not (
            bool(emitted) == bool(emitted_sha) == bool(last_emitted_rules)
        ):
            raise _contract_error(
                "FUTU_ANOMALY_CHECKPOINT_INVALID",
                "checkpoint emitted rules and emitted observation hash disagree",
            )
        states[symbol] = {
            "symbol": symbol,
            "session_date": session_date,
            "active_rule_ids": active,
            "emitted_rule_ids": emitted,
            "last_observed_at": last_observed_at,
            "last_observed_datetime": last_observed,
            "last_observation_sha256": observation_sha,
            "last_emitted_observation_sha256": emitted_sha,
            "last_emitted_rule_ids": last_emitted_rules,
        }
    return checkpoint, states


@dataclass(frozen=True, slots=True)
class FutuAnomalyProjectionResult:
    """Closed result that an adapter can wrap in ``AdapterPollResult``."""

    next_checkpoint: dict[str, Any]
    items: tuple[dict[str, Any], ...]
    duplicate_count: int
    rejected_count: int
    errors: tuple[SourcePollError, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "next_checkpoint", normalize_checkpoint(self.next_checkpoint))
        normalized_items: list[dict[str, Any]] = []
        for item in self.items:
            if type(item) is not dict:
                raise TypeError("items must contain native dictionaries")
            normalized_items.append(json.loads(canonical_json(item)))
        object.__setattr__(self, "items", tuple(normalized_items))
        for field in ("duplicate_count", "rejected_count"):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise TypeError(f"{field} must be a non-negative native integer")
        if type(self.errors) is not tuple or any(
            type(error) is not SourcePollError for error in self.errors
        ):
            raise TypeError("errors must be a tuple of SourcePollError values")

    @property
    def observed_items(self) -> tuple[dict[str, Any], ...]:
        return copy.deepcopy(self.items)

    @property
    def source_errors(self) -> tuple[SourcePollError, ...]:
        return tuple(self.errors)


def _failure(
    started_checkpoint: dict[str, Any],
    *,
    code: str,
    message: str,
    rejected_count: int = 1,
) -> FutuAnomalyProjectionResult:
    return FutuAnomalyProjectionResult(
        next_checkpoint=started_checkpoint,
        items=(),
        duplicate_count=0,
        rejected_count=rejected_count,
        errors=(SourcePollError.build(code, message[:1_000], _SCOPE),),
    )


def _rule_active(rule_id: str, metric_value: Decimal, *, was_active: bool) -> bool:
    spec = _RULE_SPECS[rule_id]
    comparison = spec["comparison"]
    threshold_key = "exit_threshold" if was_active else "entry_threshold"
    threshold = _decimal(spec[threshold_key])
    if comparison == "negative":
        return metric_value <= threshold
    return metric_value >= threshold


def _normalize_row(row: Any, *, observed_at: datetime) -> dict[str, Any]:
    if type(row) is not dict:
        raise _contract_error(
            "FUTU_ANOMALY_SNAPSHOT_INVALID",
            "every quote row must be a native object",
        )
    symbol = row.get("symbol")
    if type(symbol) is not str or symbol not in STORAGE_SYMBOLS:
        raise _contract_error(
            "FUTU_ANOMALY_SNAPSHOT_INVALID",
            "quote row symbol is outside the sealed storage universe",
        )
    if (
        row.get("quote_is_live") is not True
        or row.get("freshness_basis") != "live_20m_window"
        or row.get("research_ready") is not True
    ):
        raise _contract_error(
            "FUTU_ANOMALY_QUOTE_NOT_LIVE",
            f"{symbol} is not an admitted live research quote",
        )
    status = row.get("security_status")
    if type(status) is not str or status.strip().upper() not in {
        "NORMAL",
        "SECURITYSTATUS.NORMAL",
    }:
        raise _contract_error(
            "FUTU_ANOMALY_SECURITY_NOT_NORMAL",
            f"{symbol} lacks an explicit NORMAL security status",
        )
    if row.get("suspended") is not False:
        raise _contract_error(
            "FUTU_ANOMALY_SECURITY_SUSPENDED",
            f"{symbol} is suspended or lacks an explicit nonsuspended marker",
        )
    occurred_at, occurred = _parse_rfc3339(
        row.get("updated_at"),
        field=f"rows[{symbol}].updated_at",
    )
    if occurred > observed_at:
        raise _contract_error(
            "FUTU_ANOMALY_OBSERVATION_FUTURE",
            f"{symbol} updated_at is later than the injected observation time",
        )
    age_at_observation = (observed_at - occurred).total_seconds()
    if not 0 <= age_at_observation <= LIVE_QUOTE_MAX_AGE_SECONDS:
        raise _contract_error(
            "FUTU_ANOMALY_OBSERVATION_STALE",
            f"{symbol} is outside the live window at the injected observation time",
        )
    metrics = {
        "last": _decimal_string(row.get("last"), field=f"{symbol}.last", positive=True),
        "change_rate": _decimal_string(
            row.get("change_rate"), field=f"{symbol}.change_rate"
        ),
        "amplitude": _decimal_string(
            row.get("amplitude"), field=f"{symbol}.amplitude"
        ),
        "volume_ratio": _decimal_string(
            row.get("volume_ratio"), field=f"{symbol}.volume_ratio"
        ),
    }
    if _decimal(metrics["amplitude"]) < 0 or _decimal(metrics["volume_ratio"]) < 0:
        raise _contract_error(
            "FUTU_ANOMALY_METRIC_INVALID",
            f"{symbol} amplitude and volume_ratio must be non-negative",
        )
    session_date = occurred.astimezone(_US_EASTERN).date().isoformat()
    observation_basis = {
        "version": FUTU_ANOMALY_PROJECTION_VERSION,
        "symbol": symbol,
        "occurred_at": occurred_at,
        "session_date": session_date,
        "metrics": metrics,
        "quote_is_live": True,
        "freshness_basis": "live_20m_window",
        "research_ready": True,
        "security_status": "NORMAL",
        "suspended": False,
    }
    return {
        **observation_basis,
        "occurred_datetime": occurred,
        "observation_sha256": canonical_sha256(observation_basis),
    }


def _build_item(
    observation: dict[str, Any],
    *,
    rule_id: str,
) -> dict[str, Any]:
    spec = _RULE_SPECS[rule_id]
    symbol = observation["symbol"]
    metric_name = spec["metric"]
    occurred_at = _session_anchor(observation["session_date"])
    identity_sha = canonical_sha256({
        "version": FUTU_ANOMALY_IDENTITY_VERSION,
        "symbol": symbol,
        "session_date": observation["session_date"],
        "rule_id": rule_id,
    })
    fact = (
        f"During the US/Eastern market date {observation['session_date']}, at least "
        f"one complete admitted live Futu storage-universe snapshot satisfied the "
        f"sealed {rule_id} entry policy."
    )
    content_basis = {
        "version": FUTU_ANOMALY_PROJECTION_VERSION,
        "item_type": "market_anomaly_signal",
        "severity": "info",
        "recommended_route": "notify_only",
        "symbol": symbol,
        "us_eastern_market_date": observation["session_date"],
        "occurred_at": occurred_at,
        "rule": {
            "rule_id": rule_id,
            "metric": metric_name,
            "entry_threshold": spec["entry_threshold"],
            "exit_threshold": spec["exit_threshold"],
            "signal_direction": spec["signal_direction"],
            "unit": spec["unit"],
        },
        "fact": fact,
        "impact_hypotheses": [],
        "unknowns": _UNKNOWNS,
        "news_attribution_performed": False,
        "causal_attribution": "none",
        "signal_only": True,
    }
    content_sha = canonical_sha256(content_basis)
    return {
        "version": PROJECT_SOURCE_ITEM_VERSION,
        "external_item_id": f"futu-anomaly-{identity_sha}",
        "item_type": "market_anomaly_signal",
        "severity": "info",
        "occurred_at": occurred_at,
        "published_at": "",
        "entities": [{"kind": "security", "id": symbol, "label": symbol}],
        "headline": f"{symbol} {spec['label']} anomaly signal",
        "summary": (
            f"A sealed threshold detected a {spec['label']} anomaly for {symbol}. "
            "This is a market-data signal only; no cause is attributed."
        ),
        "facts": [{"claim": fact, "source_indexes": [0]}],
        "sources": [{
            "url": FUTU_ANOMALY_SOURCE_URL,
            "publisher": "Futu OpenAPI",
            "source_type": "readonly_market_signal",
            "published_at": "",
            "content_sha256": content_sha,
        }],
        "impact_hypotheses": [],
        "unknowns": list(_UNKNOWNS),
        "confidence": 1.0,
        "recommended_route": "notify_only",
        "extensions": {
            "futu_anomaly_v1": {
                "causal_attribution": "none",
                "content_hash_semantics": (
                    "stable_session_rule_signal_semantics_not_web_body"
                ),
                "entry_threshold": spec["entry_threshold"],
                "exit_threshold": spec["exit_threshold"],
                "metric": metric_name,
                "news_attribution_performed": False,
                "projection_version": FUTU_ANOMALY_PROJECTION_VERSION,
                "rule_id": rule_id,
                "signal_direction": spec["signal_direction"],
                "signal_only": True,
                "symbol": symbol,
                "unit": spec["unit"],
                "us_eastern_market_date": observation["session_date"],
            }
        },
    }


def project_futu_anomaly_snapshot(
    snapshot: Any,
    *,
    started_checkpoint: Any,
    observed_at_ms: Any,
) -> FutuAnomalyProjectionResult:
    """Project one complete snapshot atomically using sealed v1 rules.

    All failures return no items and the byte-equivalent normalized starting
    checkpoint.  The injected observation time is used only as a future-data
    gate; it is never included in event identity or content semantics.
    """

    try:
        safe_started = normalize_checkpoint(started_checkpoint)
    except SourceMonitoringContractError as exc:
        return _failure(
            {},
            code="FUTU_ANOMALY_CHECKPOINT_INVALID",
            message=str(exc),
        )
    try:
        _, previous_states = normalize_futu_anomaly_checkpoint(safe_started)
    except SourceMonitoringContractError as exc:
        return _failure(
            safe_started,
            code=exc.code,
            message=exc.message,
        )

    try:
        if type(observed_at_ms) is not int or type(observed_at_ms) is bool or observed_at_ms < 0:
            raise _contract_error(
                "FUTU_ANOMALY_OBSERVED_TIME_INVALID",
                "observed_at_ms must be a non-negative native integer",
            )
        observed_seconds, observed_milliseconds = divmod(observed_at_ms, 1_000)
        observed_at = datetime.fromtimestamp(
            observed_seconds,
            tz=timezone.utc,
        ) + timedelta(milliseconds=observed_milliseconds)
        if type(snapshot) is not dict:
            raise _contract_error(
                "FUTU_ANOMALY_SNAPSHOT_INVALID",
                "snapshot must be a native JSON object",
            )
        encoded_snapshot = canonical_json(snapshot).encode("utf-8")
        if len(encoded_snapshot) > MAX_FUTU_ANOMALY_SNAPSHOT_BYTES:
            raise _contract_error(
                "FUTU_ANOMALY_SNAPSHOT_TOO_LARGE",
                "snapshot exceeds the sealed 256 KiB canonical JSON limit",
            )
        _reject_unsafe_snapshot_fields(snapshot)
        admission = validate_storage_quote_snapshot(snapshot)
        if admission.get("ready") is not True:
            raise _contract_error(
                "FUTU_ANOMALY_SNAPSHOT_INVALID",
                "snapshot failed the canonical storage quote admission gate",
            )
        captured_at_text, captured_at = _parse_rfc3339(
            snapshot.get("captured_at"),
            field="snapshot.captured_at",
        )
        if captured_at > observed_at:
            raise _contract_error(
                "FUTU_ANOMALY_OBSERVATION_FUTURE",
                "snapshot captured_at is later than the injected observation time",
            )
        rows = snapshot.get("rows")
        if type(rows) is not list or len(rows) != len(STORAGE_SYMBOLS):
            raise _contract_error(
                "FUTU_ANOMALY_SNAPSHOT_INVALID",
                "snapshot rows must exactly cover the sealed storage universe",
            )
        normalized_rows = [_normalize_row(row, observed_at=observed_at) for row in rows]
        by_symbol = {row["symbol"]: row for row in normalized_rows}
        if len(by_symbol) != len(STORAGE_SYMBOLS) or set(by_symbol) != set(STORAGE_SYMBOLS):
            raise _contract_error(
                "FUTU_ANOMALY_SNAPSHOT_INVALID",
                "snapshot rows are partial, duplicated, or outside the sealed universe",
            )
        # ``captured_at_text`` is intentionally read only for validity/future gating.
        # It is not part of identity, observation hash, checkpoint, or item content.
        del captured_at_text

        for symbol in sorted(STORAGE_SYMBOLS):
            current = by_symbol[symbol]
            previous = previous_states.get(symbol)
            if previous is None:
                continue
            if current["occurred_datetime"] < previous["last_observed_datetime"]:
                raise _contract_error(
                    "FUTU_ANOMALY_OBSERVATION_REVERSED",
                    f"{symbol} observation is older than its checkpoint",
                )
            if (
                current["occurred_datetime"] == previous["last_observed_datetime"]
                and current["observation_sha256"] != previous["last_observation_sha256"]
            ):
                raise _contract_error(
                    "FUTU_ANOMALY_OBSERVATION_CONFLICT",
                    f"{symbol} reused one timestamp with different anomaly semantics",
                )
    except (SourceMonitoringContractError, OverflowError, OSError, ValueError) as exc:
        if isinstance(exc, SourceMonitoringContractError):
            code = exc.code
            message = exc.message
        else:
            code = "FUTU_ANOMALY_SNAPSHOT_INVALID"
            message = str(exc)
        return _failure(safe_started, code=code, message=message)

    items: list[dict[str, Any]] = []
    duplicate_count = 0
    next_entries: list[dict[str, Any]] = []
    try:
        for symbol in sorted(STORAGE_SYMBOLS):
            current = by_symbol[symbol]
            previous = previous_states.get(symbol)
            exact_replay = bool(
                previous
                and current["occurred_datetime"] == previous["last_observed_datetime"]
                and current["observation_sha256"] == previous["last_observation_sha256"]
            )
            if exact_replay:
                if (
                    previous["last_emitted_observation_sha256"]
                    == current["observation_sha256"]
                ):
                    duplicate_count += len(previous["last_emitted_rule_ids"])
                next_entries.append({
                    key: copy.deepcopy(previous[key])
                    for key in (
                        "symbol",
                        "session_date",
                        "active_rule_ids",
                        "emitted_rule_ids",
                        "last_observed_at",
                        "last_observation_sha256",
                        "last_emitted_observation_sha256",
                        "last_emitted_rule_ids",
                    )
                })
                continue

            same_session = bool(
                previous and previous["session_date"] == current["session_date"]
            )
            old_active = set(previous["active_rule_ids"]) if same_session else set()
            emitted = set(previous["emitted_rule_ids"]) if same_session else set()
            active: set[str] = set()
            newly_emitted: list[str] = []
            for rule_id in FUTU_ANOMALY_RULE_IDS:
                spec = _RULE_SPECS[rule_id]
                metric = _decimal(current["metrics"][spec["metric"]])
                is_active = _rule_active(
                    rule_id,
                    metric,
                    was_active=rule_id in old_active,
                )
                if is_active:
                    active.add(rule_id)
                    if rule_id not in emitted:
                        emitted.add(rule_id)
                        newly_emitted.append(rule_id)
            for rule_id in newly_emitted:
                items.append(_build_item(current, rule_id=rule_id))
            last_emitted_sha = (
                current["observation_sha256"]
                if newly_emitted
                else previous["last_emitted_observation_sha256"]
                if same_session and previous
                else ""
            )
            last_emitted_rules = (
                _sorted_rule_ids(newly_emitted)
                if newly_emitted
                else list(previous["last_emitted_rule_ids"])
                if same_session and previous
                else []
            )
            next_entries.append({
                "symbol": symbol,
                "session_date": current["session_date"],
                "active_rule_ids": _sorted_rule_ids(active),
                "emitted_rule_ids": _sorted_rule_ids(emitted),
                "last_observed_at": current["occurred_at"],
                "last_observation_sha256": current["observation_sha256"],
                "last_emitted_observation_sha256": last_emitted_sha,
                "last_emitted_rule_ids": last_emitted_rules,
            })
        next_checkpoint = {
            "version": FUTU_ANOMALY_CHECKPOINT_VERSION,
            "symbols": next_entries,
        }
        normalize_futu_anomaly_checkpoint(next_checkpoint)
        return FutuAnomalyProjectionResult(
            next_checkpoint=next_checkpoint,
            items=tuple(items),
            duplicate_count=duplicate_count,
            rejected_count=0,
            errors=(),
        )
    except SourceMonitoringContractError as exc:
        return _failure(safe_started, code=exc.code, message=exc.message)


__all__ = [
    "AMPLITUDE_RULE_ID",
    "FUTU_ANOMALY_CHECKPOINT_VERSION",
    "FUTU_ANOMALY_IDENTITY_VERSION",
    "FUTU_ANOMALY_POLICY_VERSION",
    "FUTU_ANOMALY_PROJECTION_VERSION",
    "FUTU_ANOMALY_RULE_IDS",
    "FUTU_ANOMALY_SOURCE_URL",
    "FutuAnomalyProjectionResult",
    "MAX_FUTU_ANOMALY_SNAPSHOT_BYTES",
    "PRICE_DOWN_RULE_ID",
    "PRICE_UP_RULE_ID",
    "VOLUME_RATIO_RULE_ID",
    "futu_anomaly_policy_manifest",
    "normalize_futu_anomaly_checkpoint",
    "project_futu_anomaly_snapshot",
]
