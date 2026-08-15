from __future__ import annotations

import copy
import unittest

from backend.capability_packs import CAPABILITY_PACKS
from backend.domain_adapters import StockResearchDomainAdapter
from backend.plugin_registry import plugin_registry_catalog
from backend.stock_research import (
    FIXED_STOCK_RESEARCH_BOUNDARIES,
    STOCK_EVIDENCE_CLASSES,
    STOCK_PREFLIGHT_SOURCE_TYPES,
    STOCK_RESEARCH_CAPABILITY_PACK_ID,
    STOCK_RESEARCH_CONTRACT_SCHEMA,
    STOCK_RESEARCH_CONTRACT_VERSION,
    STOCK_RESEARCH_OUTPUT_SCHEMA,
    STOCK_RESEARCH_OUTPUT_SCHEMA_SHA256,
    STOCK_RESEARCH_SCHEMA_SHA256,
    STOCK_ROOM_SCOPE_VERSION,
    StockResearchContractError,
    build_stock_research_contract,
    canonical_sha256,
    normalize_stock_room_scope,
    validate_stock_research_contract,
    validate_stock_room_scope,
)
from backend.templates import get_room_template


CUTOFF = "2026-08-12T09:30:00Z"
CONTENT_SHA = "1" * 64
SNAPSHOT_SHA = "2" * 64


def source(material_id: str, version: int = 1) -> dict:
    return {
        "source_id": f"source:{material_id}",
        "publisher": "Fixture Publisher",
        "source_uri": f"urn:ai-studio:material:{material_id}:v{version}",
        "source_sha256": CONTENT_SHA,
        "material_binding": {
            "material_id": material_id,
            "material_version": version,
            "content_sha256": CONTENT_SHA,
            "snapshot_sha256": SNAPSHOT_SHA,
        },
        "published_at_utc": "2026-08-12T08:00:00Z",
        "retrieved_at_utc": "2026-08-12T09:00:00Z",
    }


def preflight(source_type: str, symbol_slug: str) -> dict:
    return {
        "version": "stock_source_preflight_v1",
        "source_type": source_type,
        "status": "ready",
        "as_of_utc": "2026-08-12T09:00:00Z",
        "reason": "",
        "source": source(f"{symbol_slug}-{source_type}"),
    }


def symbol_row(symbol: str, *, evidence: list[dict] | None = None) -> dict:
    slug = symbol.lower().replace(":", "-").replace(".", "-")
    return {
        "symbol": symbol,
        "issuer_name": f"{symbol} issuer",
        "exchange": symbol.split(":", 1)[0],
        "currency": "USD",
        "preflight": {
            source_type: preflight(source_type, slug)
            for source_type in STOCK_PREFLIGHT_SOURCE_TYPES
        },
        "evidence": list(evidence or []),
    }


def claim(
    claim_id: str,
    symbol: str,
    evidence_class: str,
    *,
    upstream: list[str] | None = None,
) -> dict:
    material_id = f"claim-{claim_id}"
    return {
        "claim_id": claim_id,
        "symbol": symbol,
        "claim": f"Fixture {evidence_class} claim for {symbol}",
        "evidence_class": evidence_class,
        "as_of_utc": "2026-08-12T09:00:00Z",
        "source": source(material_id),
        "inference": (
            {
                "method_id": "fixture-method",
                "method_version": "1.0.0",
                "generated_at_utc": "2026-08-12T09:15:00Z",
                "upstream_claim_ids": list(upstream or []),
            }
            if evidence_class == "model_inference"
            else None
        ),
    }


def payload() -> dict:
    aapl_claims = [
        claim("aapl-official", "US:AAPL", "official_fact"),
        claim("aapl-media", "US:AAPL", "media_report"),
        claim("aapl-market", "US:AAPL", "market_proxy"),
        claim(
            "aapl-inference",
            "US:AAPL",
            "model_inference",
            upstream=["aapl-official", "aapl-media", "aapl-market"],
        ),
    ]
    return {
        "stock_room_scope": {
            "version": STOCK_ROOM_SCOPE_VERSION,
            "symbols": ["US:AAPL", "US:MSFT"],
        },
        "data_cutoff_utc": CUTOFF,
        "symbols": [
            symbol_row("US:AAPL", evidence=aapl_claims),
            symbol_row("US:MSFT"),
        ],
        "research_ready": True,
    }


class StockResearchContractTests(unittest.TestCase):
    def test_builds_closed_versioned_hashed_contract_with_all_five_preflights(self) -> None:
        contract = build_stock_research_contract(payload())

        self.assertEqual(contract["version"], STOCK_RESEARCH_CONTRACT_VERSION)
        self.assertEqual(
            contract["capability_pack_id"],
            STOCK_RESEARCH_CAPABILITY_PACK_ID,
        )
        self.assertEqual(
            set(contract["symbols"][0]["preflight"]),
            set(STOCK_PREFLIGHT_SOURCE_TYPES),
        )
        self.assertEqual(
            {item["evidence_class"] for item in contract["symbols"][0]["evidence"]},
            set(STOCK_EVIDENCE_CLASSES),
        )
        self.assertEqual(
            contract["contract_sha256"],
            canonical_sha256({
                key: value
                for key, value in contract.items()
                if key != "contract_sha256"
            }),
        )
        self.assertEqual(validate_stock_research_contract(contract), contract)

    def test_schema_and_boundaries_are_versioned_closed_and_non_executable(self) -> None:
        self.assertEqual(
            STOCK_RESEARCH_OUTPUT_SCHEMA_SHA256,
            canonical_sha256(STOCK_RESEARCH_OUTPUT_SCHEMA),
        )
        self.assertEqual(
            STOCK_RESEARCH_SCHEMA_SHA256,
            canonical_sha256(STOCK_RESEARCH_CONTRACT_SCHEMA),
        )
        self.assertFalse(STOCK_RESEARCH_CONTRACT_SCHEMA["root"]["additional_properties"])
        self.assertEqual(
            FIXED_STOCK_RESEARCH_BOUNDARIES,
            {
                "execution_capability": "none",
                "live_trading_allowed": False,
                "order_placement_allowed": False,
                "wallet_connection_allowed": False,
                "automatic_trading_allowed": False,
                "can_autonomously_decide": False,
                "can_replace_user_decision": False,
                "user_final_decision_required": True,
            },
        )

    def test_scope_is_closed_canonical_nonempty_and_room_pack_owned(self) -> None:
        normalized = normalize_stock_room_scope({
            "version": STOCK_ROOM_SCOPE_VERSION,
            "symbols": ["us:msft", " US:AAPL "],
        })
        self.assertEqual(normalized["symbols"], ["US:AAPL", "US:MSFT"])
        self.assertEqual(
            validate_stock_room_scope({
                "capability_pack_ids": [STOCK_RESEARCH_CAPABILITY_PACK_ID],
                "stock_room_scope": normalized,
            }),
            normalized,
        )
        with self.assertRaisesRegex(StockResearchContractError, "must not be empty"):
            normalize_stock_room_scope({
                "version": STOCK_ROOM_SCOPE_VERSION,
                "symbols": [],
            })
        with self.assertRaisesRegex(StockResearchContractError, "closed"):
            normalize_stock_room_scope({
                "version": STOCK_ROOM_SCOPE_VERSION,
                "symbols": ["US:AAPL"],
                "wallet": "forbidden",
            })
        with self.assertRaisesRegex(StockResearchContractError, "at most 64"):
            normalize_stock_room_scope({
                "version": STOCK_ROOM_SCOPE_VERSION,
                "symbols": [f"US:S{index}" for index in range(65)],
            })

    def test_contract_symbols_must_exactly_match_room_scope(self) -> None:
        wrong = payload()
        wrong["stock_room_scope"]["symbols"] = ["US:AAPL"]
        with self.assertRaisesRegex(StockResearchContractError, "exactly match"):
            build_stock_research_contract(wrong)

    def test_each_symbol_requires_exact_five_source_preflight(self) -> None:
        missing = payload()
        del missing["symbols"][0]["preflight"]["sec"]
        with self.assertRaisesRegex(StockResearchContractError, "closed"):
            build_stock_research_contract(missing)

        unavailable = payload()
        entry = unavailable["symbols"][0]["preflight"]["futu"]
        entry.update(status="unavailable", reason="offline", source=None)
        unavailable["research_ready"] = False
        contract = build_stock_research_contract(unavailable)
        self.assertFalse(contract["research_ready"])

    def test_material_version_is_positive_integer_and_source_hash_is_content_hash(self) -> None:
        for invalid in ("1", True, 0):
            with self.subTest(material_version=invalid):
                candidate = payload()
                candidate["symbols"][0]["preflight"]["sec"]["source"][
                    "material_binding"
                ]["material_version"] = invalid
                with self.assertRaisesRegex(StockResearchContractError, "material_version"):
                    build_stock_research_contract(candidate)

        mismatch = payload()
        mismatch["symbols"][0]["preflight"]["sec"]["source"]["source_sha256"] = "3" * 64
        with self.assertRaisesRegex(StockResearchContractError, "must equal"):
            build_stock_research_contract(mismatch)

    def test_evidence_classes_and_inference_graph_fail_closed(self) -> None:
        unknown = payload()
        unknown["symbols"][0]["evidence"][0]["evidence_class"] = "analyst_opinion"
        with self.assertRaisesRegex(StockResearchContractError, "evidence_class"):
            build_stock_research_contract(unknown)

        missing = payload()
        missing["symbols"][0]["evidence"][3]["inference"][
            "upstream_claim_ids"
        ] = ["not-present"]
        with self.assertRaisesRegex(StockResearchContractError, "missing upstream"):
            build_stock_research_contract(missing)

    def test_cutoff_and_integrity_are_enforced(self) -> None:
        future = payload()
        future["symbols"][0]["preflight"]["futu"]["as_of_utc"] = (
            "2026-08-12T09:30:01Z"
        )
        with self.assertRaisesRegex(StockResearchContractError, "data cutoff"):
            build_stock_research_contract(future)

        contract = build_stock_research_contract(payload())
        tampered = copy.deepcopy(contract)
        tampered["symbols"][0]["issuer_name"] = "Tampered issuer"
        with self.assertRaisesRegex(StockResearchContractError, "sha256 mismatch"):
            validate_stock_research_contract(tampered)

    def test_stock_pack_reuses_host_project_chain_without_storage_or_candidate_capabilities(self) -> None:
        pack = CAPABILITY_PACKS[STOCK_RESEARCH_CAPABILITY_PACK_ID]
        storage_before = CAPABILITY_PACKS["storage_research_readonly"]
        self.assertEqual(pack["dependencies"], ["structured_project_research"])
        self.assertEqual(pack["domain_adapter_ids"], ["stock_research"])
        self.assertEqual(
            pack["ui_contribution_ids"],
            ["stock_research.room_inspector/v1"],
        )
        self.assertTrue(all("candidate" not in item for item in pack["capabilities"]))
        self.assertTrue(all("simulation" not in item for item in pack["capabilities"]))
        self.assertEqual(storage_before["manifest_version"], "capability_pack_manifest_v1")
        self.assertEqual(storage_before["domain_adapter_ids"], ["storage_research"])
        self.assertIn("simulation.paper_portfolio", storage_before["capabilities"])
        self.assertEqual(
            canonical_sha256(storage_before),
            "ebcc6a2348ef9d7c8df15ec6495e764d0b0e13a47318a1a5f1f6eca39b472ac3",
        )

    def test_adapter_registry_ui_and_template_are_versioned_host_owned(self) -> None:
        adapter = StockResearchDomainAdapter()
        contract = build_stock_research_contract(payload())
        self.assertEqual(adapter.project_market_readonly_context(contract=contract), contract)
        self.assertEqual(
            adapter.declared_ports,
            frozenset({"core.market.readonly_context/v1"}),
        )
        catalog = plugin_registry_catalog()
        registry_adapter = next(
            row for row in catalog["domain_adapters"]
            if row["adapter_id"] == "stock_research"
        )
        contribution = next(
            row for row in catalog["ui_contributions"]
            if row["contribution_id"] == "stock_research.room_inspector/v1"
        )
        self.assertEqual(
            [item["port_id"] for item in registry_adapter["ports"]],
            ["core.market.readonly_context/v1"],
        )
        self.assertEqual(contribution["mode"], "host_owned_component")
        self.assertEqual(contribution["allowed_actions"], ["stock_research.inspect"])
        self.assertFalse(contribution["live_trading_allowed"])
        template = get_room_template("stock_research")
        self.assertEqual(template["domain"], "stock_research")
        self.assertEqual(len(template["members"]), 6)
        self.assertNotIn("storage_research_readonly", template["capability_pack_ids"])


if __name__ == "__main__":
    unittest.main()
