from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime

from backend.source_inbox_contracts import (
    PROJECT_SOURCE_ITEM_VERSION,
    SOURCE_IMPORT_PACKET_VERSION,
    canonical_sha256,
    normalize_source_import_packet,
)
from backend.source_monitoring.trading_impact_rules import (
    TRADING_IMPACT_RULESET_SHA256,
    TradingImpactProjection,
    TradingImpactRulesError,
    TradingImpactRulesV1,
)


CHECKED_AT = "2026-08-31T04:00:00Z"
OCCURRED_AT = "2026-08-30T13:30:00Z"
RECEIVED_AT_MS = int(datetime.fromisoformat(CHECKED_AT.replace("Z", "+00:00")).timestamp() * 1_000)


def _source(
    url: str,
    publisher: str,
    source_type: str,
    *,
    content_sha256: str = "",
    published_at: str = OCCURRED_AT,
) -> dict[str, object]:
    return {
        "url": url,
        "publisher": publisher,
        "source_type": source_type,
        "published_at": published_at,
        "content_sha256": content_sha256,
    }


def _item(
    *,
    external_item_id: str,
    item_type: str,
    entities: list[dict[str, str]],
    sources: list[dict[str, object]],
    extensions: dict[str, object],
    occurred_at: str = OCCURRED_AT,
    published_at: str = OCCURRED_AT,
) -> dict[str, object]:
    return {
        "version": PROJECT_SOURCE_ITEM_VERSION,
        "external_item_id": external_item_id,
        "item_type": item_type,
        "severity": "info",
        "occurred_at": occurred_at,
        "published_at": published_at,
        "entities": entities,
        "headline": f"Fixture {external_item_id}",
        "summary": "A deterministic local fixture for the pure Phase 5 contract.",
        "facts": [{
            "claim": "The admitted source projection records this fixture event.",
            "source_indexes": list(range(len(sources))),
        }],
        "sources": sources,
        "impact_hypotheses": [],
        "unknowns": ["Source meaning remains external_unverified."],
        "confidence": 1.0,
        "recommended_route": "notify_only",
        "extensions": extensions,
    }


def _normalized(
    raw_item: dict[str, object],
    *,
    adapter_id: str,
    source_channel: str,
) -> tuple[dict[str, object], str]:
    packet = {
        "version": SOURCE_IMPORT_PACKET_VERSION,
        "source_channel": source_channel,
        "source_key": adapter_id,
        "external_run_id": f"fixture-{adapter_id}",
        "checked_at": CHECKED_AT,
        "cutoff_at": CHECKED_AT,
        "meaningful_change": True,
        "items": [copy.deepcopy(raw_item)],
        "generation": {
            "channel": source_channel,
            "model": "",
            "cost": {
                "status": "unavailable",
                "amount": None,
                "currency": "",
                "usage_source": "not_applicable",
            },
            "correlated_output": False,
        },
    }
    normalized = normalize_source_import_packet(packet, received_at_ms=RECEIVED_AT_MS)
    item = normalized["items"][0]
    return item, canonical_sha256(item)


def _sec(
    form: str = "8-K",
    symbol: str = "US.MU",
    *,
    accepted_at: str = OCCURRED_AT,
    filing_date: str = "2026-08-30",
) -> tuple[dict[str, object], str]:
    anchor_at = accepted_at or f"{filing_date}T00:00:00Z"
    raw = _item(
        external_item_id="0000072312-26-000001",
        item_type="sec_filing",
        entities=[
            {"kind": "security", "id": symbol, "label": symbol.removeprefix("US.")},
            {"kind": "issuer", "id": "00000723125", "label": "Fixture Issuer"},
        ],
        sources=[_source(
            "https://www.sec.gov/Archives/edgar/data/723125/fixture.htm",
            "U.S. Securities and Exchange Commission EDGAR",
            "regulatory_filing",
            content_sha256="",
            published_at=anchor_at,
        )],
        extensions={
            "sec_v1": {
                "accession_number": "0000072312-26-000001",
                "accepted_at": accepted_at,
                "cik": "00000723125",
                "discovered_at_ms": RECEIVED_AT_MS,
                "filing_date": filing_date,
                "form": form,
                "items": ["2.02"] if form == "8-K" else [],
                "primary_document": "fixture.htm",
                "submissions_metadata_only": True,
                "symbol": symbol,
            }
        },
        occurred_at=anchor_at,
        published_at=anchor_at,
    )
    return _normalized(raw, adapter_id="sec_filings", source_channel="official_source_monitor")


def _ir(
    event_type: str = "earnings_release",
    *,
    is_revision: bool = False,
    symbol: str = "US.MU",
) -> tuple[dict[str, object], str]:
    projection_hash = "2" * 64
    raw = _item(
        external_item_id=f"ir-{'3' * 64}",
        item_type="company_ir_release",
        entities=[{"kind": "security", "id": symbol, "label": symbol.removeprefix("US.")}],
        sources=[
            _source(
                "https://investors.micron.com/news-releases/news-release-details/fixture",
                "Micron Technology Investor Relations",
                "company_ir",
                content_sha256="",
            ),
            _source(
                "https://investors.micron.com/rss/news-releases.xml?items=30",
                "Micron Technology Investor Relations",
                "company_ir_rss_projection",
                content_sha256=projection_hash,
            ),
        ],
        extensions={
            "company_ir_v1": {
                "event_type": event_type,
                "fiscal_period": "FY2026-Q4",
                "guid": "fixture-guid",
                "identity_kind": "guid",
                "identity_value": "fixture-guid",
                "identity_sha256": "3" * 64,
                "is_revision": is_revision,
                "previous_rss_projection_sha256": "4" * 64 if is_revision else "",
                "rss_hash_semantics": "normalized_rss_item_not_web_page_body",
                "rss_projection_sha256": projection_hash,
                "rss_projection_version": "company_ir_rss_projection_v1",
            }
        },
    )
    return _normalized(raw, adapter_id="company_ir", source_channel="official_source_monitor")


def _json_ir_raw() -> dict[str, object]:
    identity = canonical_sha256({"version": "company_ir_identity_v2", "symbol": "US.MU",
                                 "kind": "press_release_id", "value": "4945"})
    detail = "https://investors.micron.com/news/press-release/2026/fixture/default.aspx"
    endpoint = ("https://investors.micron.com/feed/PressRelease.svc/GetPressReleaseList"
                "?LanguageId=1&pageSize=30&pageNumber=0&tagList=&includeTags=true&year=-1"
                "&excludeSelection=1&bodyType=0&pressReleaseDateFilter=1"
                "&categoryId=00000000-0000-0000-0000-000000000000")
    extension = {
        "source_format": "micron_q4_public_json_v1", "event_type": "earnings_schedule",
        "fiscal_period": "", "press_release_id": 4945, "revision_number": 55457,
        "identity_kind": "press_release_id", "identity_value": "4945", "identity_sha256": identity,
        "is_revision": False, "previous_projection_sha256": "", "projection_sha256": "1" * 64,
        "projection_version": "company_ir_json_projection_v1",
        "projection_hash_semantics": "normalized_q4_item_and_newsarticle_metadata_not_article_body",
        "published_time_basis": "official_newsarticle_datePublished_v1",
        "source_declared_time_raw": "08/30/2026 14:30:00", "metadata_date_modified": OCCURRED_AT,
        "time_metadata_sha256": "2" * 64,
        "time_metadata_hash_semantics": "normalized_newsarticle_head_metadata_not_html_body",
    }
    raw = _item(external_item_id=f"ir-{identity}", item_type="company_ir_release",
                entities=[{"kind": "security", "id": "US.MU", "label": "MU"}],
                sources=[_source(detail, "Micron Technology Investor Relations", "company_ir_time_metadata", content_sha256="2" * 64),
                         _source(endpoint, "Micron Technology Investor Relations", "company_ir_json_projection", content_sha256="1" * 64)],
                extensions={"company_ir_v2": extension})
    time_hash = canonical_sha256({"version": "micron_newsarticle_time_metadata_v1", "url": detail,
                                 "headline": raw["headline"], "datePublished": OCCURRED_AT, "dateModified": OCCURRED_AT})
    extension["time_metadata_sha256"] = time_hash
    raw["sources"][0]["content_sha256"] = time_hash
    extension["projection_sha256"] = canonical_sha256({
        "version": extension["projection_version"], "symbol": "US.MU", "press_release_id": 4945,
        "revision_number": 55457, "official_url": detail, "title": raw["headline"],
        "summary": raw["summary"], "published_at": OCCURRED_AT, "metadata_date_modified": OCCURRED_AT,
        "source_declared_time_raw": extension["source_declared_time_raw"], "time_metadata_sha256": time_hash,
    })
    raw["sources"][1]["content_sha256"] = extension["projection_sha256"]
    return raw


def _macro(
    adapter_id: str,
    *,
    authority: str,
    family: str,
    subject_phase: str,
    event_state: str,
    occurrence_basis: str | None = None,
) -> tuple[dict[str, object], str]:
    schedule = subject_phase == "schedule"
    projection_hash = "5" * 64
    source_type = (
        "official_macro_calendar_projection"
        if schedule
        else "official_macro_release_projection"
    )
    occurrence_basis = occurrence_basis or (
        "official_schedule_time" if schedule else "official_release_time"
    )
    raw = _item(
        external_item_id=f"macro-{'6' * 64}",
        item_type="official_macro_schedule" if schedule else "official_macro_release",
        entities=[{"kind": "institution", "id": authority, "label": authority}],
        sources=[
            _source(
                "https://www.federalreserve.gov/newsevents/fixture.htm",
                "Official macro authority",
                "official_release_page",
                content_sha256="",
                published_at="" if schedule else OCCURRED_AT,
            ),
            _source(
                "https://www.federalreserve.gov/feeds/fixture.xml",
                "Official macro authority",
                source_type,
                content_sha256=projection_hash,
                published_at="" if schedule else OCCURRED_AT,
            ),
        ],
        extensions={
            "macro_official_v1": {
                "authority": authority,
                "data": {},
                "event_state": event_state,
                "family": family,
                "identity_sha256": "6" * 64,
                "identity_version": "official_macro_lifecycle_v1",
                "official_id": "fixture-official-id",
                "official_revision": event_state == "revised",
                "occurrence_at": OCCURRED_AT,
                "occurrence_basis": occurrence_basis,
                "previous_projection_sha256": "7" * 64 if event_state == "revised" else "",
                "projection_hash_semantics": "normalized_official_projection_not_web_body",
                "projection_sha256": projection_hash,
                "projection_version": "official_macro_projection_v1",
                "reference_period": "2026-08",
                "released_at": "" if schedule else OCCURRED_AT,
                "revision_target": (
                    "schedule_time"
                    if event_state == "revised" and schedule
                    else "data"
                    if event_state == "revised"
                    else ""
                ),
                "scheduled_at": OCCURRED_AT if schedule else "",
                "status_basis": "official_schedule_projection" if schedule else "official_released_at",
                "subject_phase": subject_phase,
            }
        },
        published_at="" if schedule else OCCURRED_AT,
    )
    return _normalized(raw, adapter_id=adapter_id, source_channel="official_source_monitor")


def _futu(upstream_rule_id: str = "price_up_5pct", symbol: str = "US.MU") -> tuple[dict[str, object], str]:
    specs = {
        "price_up_5pct": ("change_rate", "5", "4", "up_observation", "percent"),
        "price_down_5pct": ("change_rate", "-5", "-4", "down_observation", "percent"),
        "amplitude_8pct": ("amplitude", "8", "6", "none", "percent"),
        "volume_ratio_3x": ("volume_ratio", "3", "2.5", "none", "ratio"),
    }
    metric, entry, exit_value, direction, unit = specs[upstream_rule_id]
    identity_sha = canonical_sha256({
        "version": "futu_anomaly_identity_v1",
        "symbol": symbol,
        "session_date": "2026-08-30",
        "rule_id": upstream_rule_id,
    })
    raw = _item(
        external_item_id=f"futu-anomaly-{identity_sha}",
        item_type="market_anomaly_signal",
        entities=[{"kind": "security", "id": symbol, "label": symbol}],
        sources=[_source(
            "https://openapi.futunn.com/futu-api-doc/en/quote/get-market-snapshot.html",
            "Futu OpenAPI",
            "readonly_market_signal",
            content_sha256="9" * 64,
            published_at="",
        )],
        extensions={
            "futu_anomaly_v1": {
                "causal_attribution": "none",
                "content_hash_semantics": "stable_session_rule_signal_semantics_not_web_body",
                "entry_threshold": entry,
                "exit_threshold": exit_value,
                "metric": metric,
                "news_attribution_performed": False,
                "projection_version": "futu_anomaly_projection_v1",
                "rule_id": upstream_rule_id,
                "signal_direction": direction,
                "signal_only": True,
                "symbol": symbol,
                "unit": unit,
                "us_eastern_market_date": "2026-08-30",
            }
        },
        published_at="",
    )
    return _normalized(raw, adapter_id="futu_anomaly_signals", source_channel="futu_anomaly_monitor")


def _project(
    item: dict[str, object],
    item_hash: str,
    *,
    adapter_id: str,
    source_class: str = "official_source",
    source_channel: str = "official_source_monitor",
) -> TradingImpactProjection:
    return TradingImpactRulesV1.project_item(
        item,
        item_sha256=item_hash,
        adapter_id=adapter_id,
        source_class=source_class,
        source_channel=source_channel,
    )


def _rehash_projection(projection: dict[str, object]) -> dict[str, object]:
    for hypothesis in projection["hypotheses"]:
        hypothesis["hypothesis_sha256"] = canonical_sha256({
            key: value
            for key, value in hypothesis.items()
            if key != "hypothesis_sha256"
        })
    projection["projection_sha256"] = canonical_sha256({
        key: value
        for key, value in projection.items()
        if key != "projection_sha256"
    })
    return projection


class TradingImpactRulesV1Tests(unittest.TestCase):
    def test_json_ir_metadata_has_neutral_mapping_without_claiming_rss_rules(self) -> None:
        item, item_hash = _normalized(_json_ir_raw(), adapter_id="company_ir", source_channel="official_source_monitor")
        projection = _project(item, item_hash, adapter_id="company_ir").to_dict()
        self.assertEqual(projection["evaluation"], "no_match")
        self.assertEqual(projection["matched_rule_ids"], [])
        self.assertEqual(projection["hypotheses"], [])
        self.assertEqual(projection["accounting"]["provider_calls_performed"], 0)

    def test_json_ir_metadata_rejects_changed_identity_time_and_source_format(self) -> None:
        mutations = (
            lambda raw: raw["extensions"]["company_ir_v2"].update(identity_value="4946"),
            lambda raw: raw["extensions"]["company_ir_v2"].update(press_release_id=True),
            lambda raw: raw["extensions"]["company_ir_v2"].update(projection_sha256="4" * 64),
            lambda raw: raw["sources"][0].update(source_type="company_ir"),
            lambda raw: raw["sources"][0].update(published_at=CHECKED_AT),
            lambda raw: raw["sources"][0].update(url="https://example.com/announcement"),
            lambda raw: raw["sources"][1].update(url="https://investors.micron.com/rss/news-releases.xml?items=30"),
        )
        for mutate in mutations:
            raw = _json_ir_raw()
            mutate(raw)
            item, item_hash = _normalized(raw, adapter_id="company_ir", source_channel="official_source_monitor")
            with self.assertRaises(TradingImpactRulesError):
                _project(item, item_hash, adapter_id="company_ir")

    maxDiff = None

    def test_manifest_is_complete_golden_and_defensively_copied(self) -> None:
        manifest = TradingImpactRulesV1.manifest()
        self.assertEqual(TradingImpactRulesV1.ruleset_version, "trading_impact_rules_v1")
        self.assertEqual(
            TradingImpactRulesV1.ruleset_sha256,
            "28ee013d4841ff7d1f955204ae1f9fa8007b544c2a13f0bf7f5e7ee705b93603",
        )
        self.assertEqual(TradingImpactRulesV1.ruleset_sha256, TRADING_IMPACT_RULESET_SHA256)
        self.assertEqual(len(manifest["source_bindings"]), 7)
        self.assertEqual(len(manifest["rules"]), 13)
        self.assertEqual(
            manifest["source_semantics_version"],
            "trading_impact_source_semantics_v1",
        )
        self.assertEqual(
            manifest["rule_order"],
            [rule["rule_id"] for rule in manifest["rules"]],
        )
        self.assertEqual(
            manifest["sector_security_map"],
            [
                {"sector_id": "dram", "security_ids": ["US.MU"]},
                {"sector_id": "nand", "security_ids": ["US.MU", "US.SNDK"]},
                {"sector_id": "hdd", "security_ids": ["US.WDC", "US.STX"]},
            ],
        )
        manifest["rules"].clear()
        self.assertEqual(len(TradingImpactRulesV1.manifest()["rules"]), 13)

    def test_all_thirteen_rules_and_all_seven_adapters(self) -> None:
        cases: list[tuple[str, tuple[dict[str, object], str], str, str, int, list[int]]] = [
            ("sec-periodic", _sec("10-Q"), "sec_filings", "sec_periodic_filing_review_v1", 1, [0]),
            ("sec-current", _sec("8-K"), "sec_filings", "sec_current_filing_review_v1", 1, [0]),
            ("ir-revision", _ir("earnings_release", is_revision=True), "company_ir", "ir_revision_review_v1", 1, [1]),
            ("ir-schedule", _ir("earnings_schedule"), "company_ir", "ir_earnings_schedule_review_v1", 1, [1]),
            ("ir-disclosure", _ir("earnings_material"), "company_ir", "ir_earnings_disclosure_review_v1", 1, [1]),
            (
                "calendar-revision",
                _macro(
                    "official_macro_calendar",
                    authority="federal_reserve",
                    family="fomc_meeting",
                    subject_phase="schedule",
                    event_state="revised",
                ),
                "official_macro_calendar",
                "macro_schedule_revision_review_v1",
                3,
                [1],
            ),
            (
                "calendar-schedule",
                _macro(
                    "official_macro_calendar",
                    authority="bls",
                    family="consumer_price_index",
                    subject_phase="schedule",
                    event_state="scheduled",
                ),
                "official_macro_calendar",
                "macro_schedule_review_v1",
                3,
                [1],
            ),
            (
                "treasury-revision",
                _macro(
                    "treasury_releases",
                    authority="treasury",
                    family="debt_to_penny",
                    subject_phase="release",
                    event_state="revised",
                ),
                "treasury_releases",
                "macro_release_revision_review_v1",
                3,
                [1],
            ),
            (
                "fed-release",
                _macro(
                    "federal_reserve",
                    authority="federal_reserve",
                    family="monetary_policy",
                    subject_phase="release",
                    event_state="released",
                ),
                "federal_reserve",
                "macro_release_review_v1",
                3,
                [1],
            ),
            (
                "bls-release",
                _macro(
                    "bls_releases",
                    authority="bls",
                    family="employment_situation",
                    subject_phase="release",
                    event_state="released",
                ),
                "bls_releases",
                "macro_release_review_v1",
                3,
                [1],
            ),
            ("futu-up", _futu("price_up_5pct"), "futu_anomaly_signals", "futu_price_up_condition_review_v1", 1, [0]),
            ("futu-down", _futu("price_down_5pct"), "futu_anomaly_signals", "futu_price_down_condition_review_v1", 1, [0]),
            ("futu-range", _futu("amplitude_8pct"), "futu_anomaly_signals", "futu_range_condition_review_v1", 1, [0]),
            ("futu-activity", _futu("volume_ratio_3x"), "futu_anomaly_signals", "futu_market_activity_condition_review_v1", 1, [0]),
        ]
        seen_rules: set[str] = set()
        seen_adapters: set[str] = set()
        for label, (item, item_hash), adapter_id, expected_rule, count, indexes in cases:
            with self.subTest(label=label):
                before = copy.deepcopy(item)
                readonly_market = adapter_id == "futu_anomaly_signals"
                projection = _project(
                    item,
                    item_hash,
                    adapter_id=adapter_id,
                    source_class="readonly_market" if readonly_market else "official_source",
                    source_channel="futu_anomaly_monitor" if readonly_market else "official_source_monitor",
                ).to_dict()
                self.assertEqual(item, before)
                self.assertEqual(projection["evaluation"], "matched")
                self.assertEqual(projection["matched_rule_ids"], [expected_rule])
                self.assertEqual(len(projection["hypotheses"]), count)
                self.assertEqual(
                    {tuple(row["impact_hypothesis"]["source_indexes"]) for row in projection["hypotheses"]},
                    {tuple(indexes)},
                )
                for row in projection["hypotheses"]:
                    impact = row["impact_hypothesis"]
                    self.assertIs(type(impact["confidence"]), float)
                    self.assertEqual(impact["confidence"], 0.5)
                    self.assertEqual(row["confidence_basis"]["numerator"], 2)
                    self.assertEqual(row["confidence_basis"]["denominator"], 4)
                    self.assertFalse(row["confidence_basis"]["outcome_probability"])
                    self.assertEqual(row["counterevidence"]["status"], "unknown")
                    self.assertEqual(row["counterevidence"]["source_indexes"], [])
                self.assertEqual(
                    projection["accounting"],
                    {
                        "scope": "trading_impact_engine_only",
                        "model_calls_performed": 0,
                        "provider_calls_performed": 0,
                        "network_requests_performed": 0,
                        "market_calls_performed": 0,
                        "database_writes_performed": 0,
                    },
                )
                self.assertFalse(projection["interpretation_boundary"]["directional_forecast"])
                self.assertEqual(projection["interpretation_boundary"]["execution_authority"], "none")
                seen_rules.add(expected_rule)
                seen_adapters.add(adapter_id)
        self.assertEqual(seen_rules, set(TradingImpactRulesV1.manifest()["rule_order"]))
        self.assertEqual(
            seen_adapters,
            {
                "sec_filings",
                "company_ir",
                "federal_reserve",
                "bls_releases",
                "treasury_releases",
                "official_macro_calendar",
                "futu_anomaly_signals",
            },
        )

    def test_sec_acceptance_and_date_only_anchors_keep_honest_precision(self) -> None:
        accepted_item, accepted_hash = _sec("8-K")
        accepted = _project(
            accepted_item,
            accepted_hash,
            adapter_id="sec_filings",
        ).to_dict()
        accepted_semantic = accepted["source_item_binding"][
            "source_semantic_binding"
        ]
        self.assertEqual(accepted_semantic["anchor_at"], OCCURRED_AT)
        self.assertEqual(accepted_semantic["anchor_semantics"], "sec_acceptance_time")
        self.assertEqual(accepted_semantic["precision"], "timestamp")
        self.assertEqual(
            accepted["hypotheses"][0]["time_dimension"],
            {
                "horizon_id": "reporting_window",
                "anchor_at": OCCURRED_AT,
                "anchor_semantics": "sec_acceptance_time",
                "precision": "timestamp",
            },
        )

        date_item, date_hash = _sec("10-Q", accepted_at="")
        date_projection = _project(
            date_item,
            date_hash,
            adapter_id="sec_filings",
        ).to_dict()
        date_semantic = date_projection["source_item_binding"][
            "source_semantic_binding"
        ]
        self.assertEqual(date_semantic["anchor_at"], "2026-08-30T00:00:00Z")
        self.assertEqual(
            date_semantic["anchor_semantics"],
            "sec_filing_date_anchor_not_exact_time",
        )
        self.assertEqual(date_semantic["precision"], "date_anchor")
        self.assertEqual(
            date_projection["hypotheses"][0]["time_dimension"]["precision"],
            "date_anchor",
        )

    def test_exact_neutral_wording_sector_order_and_time_semantics(self) -> None:
        sec_item, sec_hash = _sec("8-K", "US.MU")
        sec = _project(sec_item, sec_hash, adapter_id="sec_filings").to_dict()
        self.assertEqual(
            sec["hypotheses"][0]["impact_hypothesis"]["statement"],
            "The admitted SEC 8-K metadata for US.MU may require review of current "
            "issuer-event assumptions; the form alone does not imply market direction "
            "or magnitude.",
        )
        macro_item, macro_hash = _macro(
            "official_macro_calendar",
            authority="treasury",
            family="debt_to_penny",
            subject_phase="schedule",
            event_state="scheduled",
            occurrence_basis="official_schedule_time",
        )
        macro = _project(macro_item, macro_hash, adapter_id="official_macro_calendar").to_dict()
        self.assertEqual(
            [row["affected_area_binding"]["id"] for row in macro["hypotheses"]],
            ["dram", "nand", "hdd"],
        )
        self.assertEqual(
            [row["affected_area_binding"]["security_ids"] for row in macro["hypotheses"]],
            [["US.MU"], ["US.MU", "US.SNDK"], ["US.WDC", "US.STX"]],
        )
        self.assertTrue(all(row["time_dimension"]["precision"] == "timestamp" for row in macro["hypotheses"]))
        rendered = json.dumps(macro, ensure_ascii=False).lower()
        for forbidden in (
            "buy",
            "sell",
            "bullish",
            "bearish",
            "target price",
            "expected return",
            "profit opportunity",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_company_ir_other_is_a_deterministic_no_match(self) -> None:
        item, item_hash = _ir("other")
        first = _project(item, item_hash, adapter_id="company_ir")
        second = _project(item, item_hash, adapter_id="company_ir")
        self.assertEqual(first.to_dict(), second.to_dict())
        projection = first.to_dict()
        self.assertEqual(projection["evaluation"], "no_match")
        self.assertEqual(projection["matched_rule_ids"], [])
        self.assertEqual(projection["hypotheses"], [])

    def test_projection_is_deeply_defensive_and_all_hashes_are_bound(self) -> None:
        item, item_hash = _futu("amplitude_8pct")
        projection = _project(
            item,
            item_hash,
            adapter_id="futu_anomaly_signals",
            source_class="readonly_market",
            source_channel="futu_anomaly_monitor",
        )
        first = projection.to_dict()
        rebuilt = TradingImpactProjection.build(first)
        self.assertEqual(first, rebuilt.to_dict())
        first["hypotheses"][0]["counterevidence"]["status"] = "observed"
        self.assertEqual(projection.to_dict()["hypotheses"][0]["counterevidence"]["status"], "unknown")

        tampered = rebuilt.to_dict()
        tampered["accounting"]["model_calls_performed"] = 1
        with self.assertRaises(TradingImpactRulesError) as context:
            TradingImpactProjection.build(tampered)
        self.assertEqual(context.exception.code, "TRADING_IMPACT_ACCOUNTING_INVALID")

        tampered = rebuilt.to_dict()
        tampered["projection_key_sha256"] = "0" * 64
        with self.assertRaises(TradingImpactRulesError) as context:
            TradingImpactProjection.build(tampered)
        self.assertEqual(context.exception.code, "TRADING_IMPACT_PROJECTION_KEY_INVALID")

    def test_hash_valid_semantic_tampering_is_rejected(self) -> None:
        item, item_hash = _sec("8-K")
        projection = _project(item, item_hash, adapter_id="sec_filings").to_dict()

        wrong_rule = copy.deepcopy(projection)
        wrong_rule["matched_rule_ids"] = ["sec_periodic_filing_review_v1"]
        hypothesis = wrong_rule["hypotheses"][0]
        hypothesis["rule_id"] = "sec_periodic_filing_review_v1"
        hypothesis["impact_hypothesis"]["time_horizon"] = "reporting_cycle"
        hypothesis["time_dimension"]["horizon_id"] = "reporting_cycle"
        hypothesis["transmission_mechanism"] = "issuer_periodic_disclosure_review"
        hypothesis["impact_hypothesis"]["statement"] = (
            "The admitted SEC 10-K metadata for US.MU may require review of existing "
            "issuer assumptions; the form alone does not imply market direction or magnitude."
        )
        _rehash_projection(wrong_rule)
        with self.assertRaises(TradingImpactRulesError) as wrong_rule_error:
            TradingImpactProjection.build(wrong_rule)
        self.assertEqual(wrong_rule_error.exception.code, "TRADING_IMPACT_RULE_INVALID")

        wrong_symbol = copy.deepcopy(projection)
        wrong_symbol["hypotheses"][0]["impact_hypothesis"]["statement"] = (
            "The admitted SEC 8-K metadata for US.WDC may require review of current "
            "issuer-event assumptions; the form alone does not imply market direction or magnitude."
        )
        _rehash_projection(wrong_symbol)
        with self.assertRaises(TradingImpactRulesError) as wrong_symbol_error:
            TradingImpactProjection.build(wrong_symbol)
        self.assertEqual(
            wrong_symbol_error.exception.code,
            "TRADING_IMPACT_STATEMENT_INVALID",
        )

        wrong_time = copy.deepcopy(projection)
        wrong_time["hypotheses"][0]["time_dimension"]["anchor_at"] = (
            "2025-01-01T00:00:00Z"
        )
        _rehash_projection(wrong_time)
        with self.assertRaises(TradingImpactRulesError) as wrong_time_error:
            TradingImpactProjection.build(wrong_time)
        self.assertEqual(wrong_time_error.exception.code, "TRADING_IMPACT_TIME_INVALID")

        macro_item, macro_hash = _macro(
            "federal_reserve",
            authority="federal_reserve",
            family="monetary_policy",
            subject_phase="release",
            event_state="released",
        )
        macro = _project(
            macro_item,
            macro_hash,
            adapter_id="federal_reserve",
        ).to_dict()
        wrong_index = copy.deepcopy(macro)
        wrong_index["hypotheses"][0]["impact_hypothesis"]["source_indexes"] = [0]
        _rehash_projection(wrong_index)
        with self.assertRaises(TradingImpactRulesError) as wrong_index_error:
            TradingImpactProjection.build(wrong_index)
        self.assertEqual(
            wrong_index_error.exception.code,
            "TRADING_IMPACT_SOURCE_INDEX_INVALID",
        )

    def test_parent_and_binding_fail_closed_without_mutation(self) -> None:
        item, item_hash = _sec("10-K")
        original = copy.deepcopy(item)
        with self.assertRaises(TradingImpactRulesError) as context:
            _project(item, "0" * 64, adapter_id="sec_filings")
        self.assertEqual(context.exception.code, "TRADING_IMPACT_ITEM_HASH_INVALID")
        self.assertEqual(item, original)

        with self.assertRaises(TradingImpactRulesError) as context:
            _project(
                item,
                item_hash,
                adapter_id="sec_filings",
                source_class="readonly_market",
                source_channel="futu_anomaly_monitor",
            )
        self.assertEqual(context.exception.code, "TRADING_IMPACT_SOURCE_BINDING_INVALID")

        polluted = copy.deepcopy(item)
        polluted["impact_hypotheses"] = [{
            "statement": "Caller-owned hypothesis.",
            "affected_area": "security:US.MU",
            "time_horizon": "unknown",
            "source_indexes": [0],
            "confidence": 0.5,
        }]
        polluted_hash = canonical_sha256(polluted)
        with self.assertRaises(TradingImpactRulesError) as context:
            _project(polluted, polluted_hash, adapter_id="sec_filings")
        self.assertEqual(context.exception.code, "TRADING_IMPACT_PARENT_HYPOTHESES_PRESENT")

    def test_strict_native_types_source_selectors_and_policy_drift_fail(self) -> None:
        item, _item_hash = _ir("earnings_release")
        forged = copy.deepcopy(item)
        forged["sources"][0]["source_type"] = "company_ir_rss_projection"
        forged_hash = canonical_sha256(forged)
        with self.assertRaises(TradingImpactRulesError) as context:
            _project(forged, forged_hash, adapter_id="company_ir")
        self.assertEqual(context.exception.code, "TRADING_IMPACT_SOURCE_BINDING_INVALID")

        futu, _futu_hash = _futu("volume_ratio_3x")
        drifted = copy.deepcopy(futu)
        drifted["extensions"]["futu_anomaly_v1"]["entry_threshold"] = "2.9"
        drifted_hash = canonical_sha256(drifted)
        with self.assertRaises(TradingImpactRulesError) as context:
            _project(
                drifted,
                drifted_hash,
                adapter_id="futu_anomaly_signals",
                source_class="readonly_market",
                source_channel="futu_anomaly_monitor",
            )
        self.assertEqual(context.exception.code, "TRADING_IMPACT_FUTU_INVALID")

        sec, _sec_hash = _sec("8-K")
        boolean_confidence = copy.deepcopy(sec)
        boolean_confidence["confidence"] = True
        boolean_hash = canonical_sha256(boolean_confidence)
        with self.assertRaises(TradingImpactRulesError) as context:
            _project(boolean_confidence, boolean_hash, adapter_id="sec_filings")
        self.assertEqual(context.exception.code, "TRADING_IMPACT_PARENT_BOUNDARY_INVALID")

    def test_volatile_discovery_metadata_does_not_change_rule_or_hypothesis(self) -> None:
        first_item, first_hash = _sec("8-K")
        second_item = copy.deepcopy(first_item)
        second_item["extensions"]["sec_v1"]["discovered_at_ms"] += 1
        second_hash = canonical_sha256(second_item)
        first = _project(first_item, first_hash, adapter_id="sec_filings").to_dict()
        second = _project(second_item, second_hash, adapter_id="sec_filings").to_dict()
        self.assertNotEqual(first["projection_key_sha256"], second["projection_key_sha256"])
        self.assertEqual(first["matched_rule_ids"], second["matched_rule_ids"])
        self.assertEqual(first["hypotheses"], second["hypotheses"])


if __name__ == "__main__":
    unittest.main()
