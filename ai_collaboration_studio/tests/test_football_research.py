from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from backend.football_research import (
    FIXED_FOOTBALL_RESEARCH_BOUNDARIES,
    FOOTBALL_EVIDENCE_CLASSES,
    FOOTBALL_PROBABILITY_STATE,
    FOOTBALL_RESEARCH_CAPABILITY_PACK_ID,
    FOOTBALL_RESEARCH_CONTRACT_SCHEMA,
    FOOTBALL_RESEARCH_CONTRACT_VERSION,
    FOOTBALL_RESEARCH_OUTPUT_SCHEMA,
    FOOTBALL_RESEARCH_OUTPUT_SCHEMA_SHA256,
    FOOTBALL_RESEARCH_SCHEMA_SHA256,
    FootballResearchContractError,
    build_football_research_contract,
    canonical_sha256,
    validate_football_research_contract,
    verify_football_research_contract,
)


CUTOFF = "2026-08-12T10:00:00Z"
KICKOFF = "2026-08-13T19:00:00Z"


def source(
    evidence_class: str,
    *,
    source_id: str,
    upstream_claim_ids: list[str] | None = None,
    publication_state: str | None = None,
    use_https: bool = False,
) -> dict:
    material_id = f"material-{source_id}"
    content_sha256 = canonical_sha256({"material_id": material_id, "version": 1})
    binding = {
        "material_id": material_id,
        "material_version": 1,
        "content_sha256": content_sha256,
        "snapshot_sha256": canonical_sha256({
            "material_id": material_id,
            "version": 1,
            "snapshot": True,
        }),
    }
    if evidence_class == "odds_proxy":
        publication = {
            "state": "observed",
            "published_at_utc": None,
            "observed_at_utc": "2026-08-12T09:00:00Z",
        }
    else:
        state = publication_state or (
            "not_published" if evidence_class == "model_inference" else "published"
        )
        publication = {
            "state": state,
            "published_at_utc": (
                "2026-08-12T08:00:00Z" if state == "published" else None
            ),
            "observed_at_utc": None,
        }
    result = {
        "source_id": source_id,
        "publisher": "Chance Research Fixture Publisher",
        "source_uri": (
            f"https://evidence.example.test/{source_id}"
            if use_https
            else f"urn:ai-studio:material:{material_id}:v1"
        ),
        "source_sha256": content_sha256,
        "material_binding": binding,
        "publication": publication,
        "retrieved_at_utc": "2026-08-12T09:30:00Z",
    }
    if evidence_class == "model_inference":
        result["inference"] = {
            "method_id": "fixture-research-method",
            "method_version": "1.0.0",
            "generated_at_utc": "2026-08-12T09:15:00Z",
            "upstream_claim_ids": list(upstream_claim_ids or []),
        }
    return result


def evidence_field(
    value,
    evidence_class: str = "official_fact",
    *,
    source_id: str,
    claim_id: str | None = None,
    upstream_claim_ids: list[str] | None = None,
    publication_state: str | None = None,
    use_https: bool = False,
) -> dict:
    resolved_claim_id = claim_id or (
        f"claim-{source_id}-{canonical_sha256(value)[:12]}"
    )
    return {
        "claim_id": resolved_claim_id,
        "value": value,
        "evidence_class": evidence_class,
        "as_of_utc": (
            "2026-08-12T09:20:00Z"
            if evidence_class == "model_inference"
            else "2026-08-12T09:00:00Z"
        ),
        "source": source(
            evidence_class,
            source_id=source_id,
            upstream_claim_ids=upstream_claim_ids,
            publication_state=publication_state,
            use_https=use_https,
        ),
    }


def fixture_history(team_id: str, role: str) -> list[dict]:
    roles = ["away", "home", "away"] if role == "home" else ["home", "away", "home"]
    return [
        {
            "match_id": f"{team_id}-m1",
            "kickoff_utc": "2026-08-01T19:00:00Z",
            "venue": {"venue_id": f"{team_id}-v1", "venue_name": f"{team_id} Ground One"},
            "role": roles[0],
        },
        {
            "match_id": f"{team_id}-m2",
            "kickoff_utc": "2026-08-06T19:00:00Z",
            "venue": {"venue_id": f"{team_id}-v2", "venue_name": f"{team_id} Ground Two"},
            "role": roles[1],
        },
        {
            "match_id": f"{team_id}-m3",
            "kickoff_utc": "2026-08-10T19:00:00Z",
            "venue": {"venue_id": f"{team_id}-v3", "venue_name": f"{team_id} Ground Three"},
            "role": roles[2],
        },
    ]


def team_context(team_id: str, team_name: str, role: str) -> dict:
    fixtures = fixture_history(team_id, role)
    history = evidence_field(
        fixtures,
        source_id=f"history-{team_id}",
        claim_id=f"claim-history-{team_id}",
    )
    history_claim = history["claim_id"]
    recent_ids = [fixture["match_id"] for fixture in fixtures]
    travel_distance = 12.4 if role == "home" else 415.2
    return {
        "team_id": evidence_field(team_id, source_id=f"official-{team_id}-id"),
        "team_name": evidence_field(team_name, source_id=f"official-{team_id}-name"),
        "match_role": role,
        "schedule_context": {
            "fixture_history": history,
            "fixtures_last_7d": evidence_field(
                {
                    "window_start_utc": "2026-08-05T10:00:00Z",
                    "window_end_utc": CUTOFF,
                    "fixture_ids": [f"{team_id}-m2", f"{team_id}-m3"],
                    "count": 2,
                },
                "model_inference",
                source_id=f"window-7d-{team_id}",
                upstream_claim_ids=[history_claim],
            ),
            "fixtures_last_14d": evidence_field(
                {
                    "window_start_utc": "2026-07-29T10:00:00Z",
                    "window_end_utc": CUTOFF,
                    "fixture_ids": recent_ids,
                    "count": 3,
                },
                "model_inference",
                source_id=f"window-14d-{team_id}",
                upstream_claim_ids=[history_claim],
            ),
            "rest_hours_before_kickoff": evidence_field(
                72.0,
                "model_inference",
                source_id=f"rest-{team_id}",
                upstream_claim_ids=[history_claim],
            ),
            "travel": evidence_field(
                {
                    "origin": {
                        "venue_id": fixtures[-1]["venue"]["venue_id"],
                        "venue_name": fixtures[-1]["venue"]["venue_name"],
                    },
                    "destination": {
                        "venue_id": "venue-target",
                        "venue_name": "Fixture Stadium",
                    },
                    "distance_km": travel_distance,
                    "method": "geodesic_haversine",
                },
                "model_inference",
                source_id=f"travel-{team_id}",
                upstream_claim_ids=[history_claim],
            ),
            "home_away_sequence": evidence_field(
                [
                    {"match_id": fixture["match_id"], "role": fixture["role"]}
                    for fixture in fixtures
                ],
                source_id=f"sequence-{team_id}",
            ),
        },
        "availability": {
            "lineup": evidence_field(
                {"publication_state": "not_published", "players": []},
                source_id=f"lineup-{team_id}",
                publication_state="not_published",
            ),
            "injuries": evidence_field(
                {
                    "publication_state": "published",
                    "entries": [{
                        "player_id": f"{team_id}-player-1",
                        "player_name": f"Chance {team_name}",
                        "status": "questionable",
                        "detail": "Late fitness check announced by the club.",
                    }],
                },
                "media_report",
                source_id=f"injury-{team_id}",
            ),
            "suspensions": evidence_field(
                {"publication_state": "published", "entries": []},
                source_id=f"suspension-{team_id}",
            ),
        },
        "tactical_context": evidence_field(
            [
                "Chance creation is concentrated in wide areas.",
                "No calibrated probability is available.",
            ],
            "model_inference",
            source_id=f"tactics-{team_id}",
            upstream_claim_ids=[history_claim],
        ),
        "recent_performance": {
            "fixture_ids": evidence_field(
                recent_ids,
                source_id=f"recent-ids-{team_id}",
            ),
            "results_sequence": evidence_field(
                [
                    {"match_id": recent_ids[0], "result": "W"},
                    {"match_id": recent_ids[1], "result": "D"},
                    {"match_id": recent_ids[2], "result": "L"},
                ],
                source_id=f"recent-results-{team_id}",
            ),
            "performance_notes": evidence_field(
                [
                    {"match_id": match_id, "note": "Observed performance note at the common cutoff."}
                    for match_id in recent_ids
                ],
                "media_report",
                source_id=f"performance-{team_id}",
            ),
        },
    }


def payload() -> dict:
    return {
        "match_identity": {
            "competition_id": evidence_field(
                "eng.premier_league",
                source_id="match-competition-id",
            ),
            "competition": evidence_field("Fixture Premier League", source_id="match-competition"),
            "season": evidence_field("2026-27", source_id="match-season"),
            "match_id": evidence_field("fixture-match-0001", source_id="match-id"),
            "kickoff_utc": evidence_field(KICKOFF, source_id="match-kickoff"),
            "venue_id": evidence_field("venue-target", source_id="match-venue-id"),
            "venue": evidence_field("Fixture Stadium", source_id="match-venue-name"),
            "home_team_id": evidence_field("team-home", source_id="match-home-id"),
            "home_team_name": evidence_field("Fixture Home", source_id="match-home-name"),
            "away_team_id": evidence_field("team-away", source_id="match-away-id"),
            "away_team_name": evidence_field("Fixture Away", source_id="match-away-name"),
        },
        "data_cutoff_utc": CUTOFF,
        "teams": {
            "home": team_context("team-home", "Fixture Home", "home"),
            "away": team_context("team-away", "Fixture Away", "away"),
        },
        "odds_proxies": [
            evidence_field(
                {
                    "market": "three_way_full_time",
                    "selection": "home",
                    "decimal_odds": 2.15,
                },
                "odds_proxy",
                source_id="odds-home-fixture",
            )
        ],
    }


class FootballResearchContractTests(unittest.TestCase):
    def test_build_seals_complete_readonly_contract_and_canonical_hash(self) -> None:
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            contract = build_football_research_contract(payload())

        self.assertEqual(contract["version"], FOOTBALL_RESEARCH_CONTRACT_VERSION)
        self.assertEqual(contract["capability_pack_id"], FOOTBALL_RESEARCH_CAPABILITY_PACK_ID)
        self.assertEqual(contract["probability_state"], FOOTBALL_PROBABILITY_STATE)
        self.assertIs(contract["future_probability_available"], False)
        self.assertIs(contract["probability_metrics_visible"], False)
        self.assertIs(contract["odds_are_proxy_only"], True)
        for field, expected in FIXED_FOOTBALL_RESEARCH_BOUNDARIES.items():
            self.assertIs(type(contract[field]), type(expected))
            self.assertEqual(contract[field], expected)
        sealed = dict(contract)
        stored_sha256 = sealed.pop("contract_sha256")
        self.assertEqual(stored_sha256, canonical_sha256(sealed))
        self.assertEqual(validate_football_research_contract(contract), contract)
        self.assertEqual(verify_football_research_contract(contract), contract)
        self.assertEqual(canonical_sha256(dict(reversed(list(sealed.items())))), stored_sha256)

    def test_schema_is_versioned_recursively_closed_and_hash_sealed(self) -> None:
        self.assertEqual(FOOTBALL_RESEARCH_CONTRACT_VERSION, "football_research_contract_v1")
        self.assertIs(FOOTBALL_RESEARCH_OUTPUT_SCHEMA["additional_properties"], False)
        self.assertEqual(
            set(FOOTBALL_RESEARCH_OUTPUT_SCHEMA["required"]),
            set(FOOTBALL_RESEARCH_OUTPUT_SCHEMA["fields"]),
        )
        self.assertEqual(
            FOOTBALL_RESEARCH_OUTPUT_SCHEMA_SHA256,
            canonical_sha256(FOOTBALL_RESEARCH_OUTPUT_SCHEMA),
        )
        self.assertEqual(
            FOOTBALL_RESEARCH_SCHEMA_SHA256,
            canonical_sha256(FOOTBALL_RESEARCH_CONTRACT_SCHEMA),
        )
        self.assertTrue(all(
            definition["additional_properties"] is False
            for definition in FOOTBALL_RESEARCH_CONTRACT_SCHEMA["definitions"].values()
        ))

    def test_fixture_history_windows_and_rest_are_exactly_recomputable(self) -> None:
        contract = build_football_research_contract(payload())
        schedule = contract["teams"]["home"]["schedule_context"]
        self.assertEqual(
            schedule["fixtures_last_7d"]["value"]["fixture_ids"],
            ["team-home-m2", "team-home-m3"],
        )
        self.assertEqual(schedule["fixtures_last_14d"]["value"]["count"], 3)
        self.assertEqual(schedule["rest_hours_before_kickoff"]["value"], 72.0)
        self.assertNotIn(
            contract["match_identity"]["match_id"]["value"],
            [item["match_id"] for item in schedule["fixture_history"]["value"]],
        )

        wrong_count = payload()
        wrong_count["teams"]["home"]["schedule_context"]["fixtures_last_7d"]["value"]["count"] = 1
        with self.assertRaisesRegex(FootballResearchContractError, "count must equal"):
            build_football_research_contract(wrong_count)

        wrong_window = payload()
        wrong_window["teams"]["home"]["schedule_context"]["fixtures_last_14d"]["value"]["fixture_ids"].pop()
        with self.assertRaisesRegex(FootballResearchContractError, "exactly match"):
            build_football_research_contract(wrong_window)

        wrong_rest = payload()
        wrong_rest["teams"]["home"]["schedule_context"]["rest_hours_before_kickoff"]["value"] = 71.9
        with self.assertRaisesRegex(FootballResearchContractError, "latest-fixture interval"):
            build_football_research_contract(wrong_rest)

    def test_target_venue_and_match_id_are_cross_sealed(self) -> None:
        contract = build_football_research_contract(payload())
        self.assertEqual(
            contract["match_identity"]["competition_id"]["value"],
            "eng.premier_league",
        )

        missing_competition_id = payload()
        del missing_competition_id["match_identity"]["competition_id"]
        with self.assertRaisesRegex(FootballResearchContractError, "competition_id"):
            build_football_research_contract(missing_competition_id)

        missing_venue_id = payload()
        del missing_venue_id["match_identity"]["venue_id"]
        with self.assertRaisesRegex(FootballResearchContractError, "venue_id"):
            build_football_research_contract(missing_venue_id)

        wrong_destination = payload()
        destination = wrong_destination["teams"]["away"]["schedule_context"]["travel"]["value"]["destination"]
        destination["venue_id"] = "wrong-venue"
        with self.assertRaisesRegex(FootballResearchContractError, "target venue"):
            build_football_research_contract(wrong_destination)

        target_in_history = payload()
        target_in_history["teams"]["home"]["schedule_context"]["fixture_history"]["value"][0]["match_id"] = "fixture-match-0001"
        with self.assertRaisesRegex(FootballResearchContractError, "exclude the target match"):
            build_football_research_contract(target_in_history)

    def test_home_away_and_recent_performance_reuse_fixture_ids(self) -> None:
        wrong_role_ref = payload()
        wrong_role_ref["teams"]["home"]["schedule_context"]["home_away_sequence"]["value"][0]["role"] = "neutral"
        with self.assertRaisesRegex(FootballResearchContractError, "exactly reuse"):
            build_football_research_contract(wrong_role_ref)

        wrong_result_ref = payload()
        wrong_result_ref["teams"]["home"]["recent_performance"]["results_sequence"]["value"][1]["match_id"] = "invented-match"
        with self.assertRaisesRegex(FootballResearchContractError, "exactly reuse"):
            build_football_research_contract(wrong_result_ref)

        non_recent_ids = payload()
        non_recent_ids["teams"]["home"]["recent_performance"]["fixture_ids"]["value"] = ["team-home-m1", "team-home-m2"]
        with self.assertRaisesRegex(FootballResearchContractError, "recent suffix"):
            build_football_research_contract(non_recent_ids)

    def test_every_evidence_claim_id_is_globally_unique(self) -> None:
        duplicate = payload()
        duplicate["match_identity"]["season"]["claim_id"] = duplicate["match_identity"]["competition"]["claim_id"]
        with self.assertRaisesRegex(FootballResearchContractError, "globally unique"):
            build_football_research_contract(duplicate)

    def test_material_binding_accepts_exact_local_urn_or_https_only(self) -> None:
        https_payload = payload()
        field = https_payload["match_identity"]["competition"]
        field["source"] = source(
            "official_fact",
            source_id="match-competition-https",
            use_https=True,
        )
        self.assertEqual(
            build_football_research_contract(https_payload)["match_identity"]["competition"]["source"]["source_uri"],
            "https://evidence.example.test/match-competition-https",
        )

        wrong_urn = payload()
        wrong_urn["match_identity"]["competition"]["source"]["source_uri"] = "urn:ai-studio:material:other:v1"
        with self.assertRaisesRegex(FootballResearchContractError, "exact"):
            build_football_research_contract(wrong_urn)

        wrong_content = payload()
        wrong_content["match_identity"]["competition"]["source"]["material_binding"]["content_sha256"] = "0" * 64
        with self.assertRaisesRegex(FootballResearchContractError, "must equal"):
            build_football_research_contract(wrong_content)

        insecure = payload()
        insecure["match_identity"]["competition"]["source"]["source_uri"] = "http://evidence.example.test/item"
        with self.assertRaisesRegex(FootballResearchContractError, "HTTPS"):
            build_football_research_contract(insecure)

    def test_model_inference_requires_existing_acyclic_upstream_claims(self) -> None:
        missing = payload()
        inference = missing["teams"]["home"]["tactical_context"]["source"]["inference"]
        inference["upstream_claim_ids"] = ["claim-does-not-exist"]
        with self.assertRaisesRegex(FootballResearchContractError, "missing upstream"):
            build_football_research_contract(missing)

        self_reference = payload()
        field = self_reference["teams"]["home"]["tactical_context"]
        field["source"]["inference"]["upstream_claim_ids"] = [field["claim_id"]]
        with self.assertRaisesRegex(FootballResearchContractError, "reference itself"):
            build_football_research_contract(self_reference)

        cycle = payload()
        home = cycle["teams"]["home"]["tactical_context"]
        away = cycle["teams"]["away"]["tactical_context"]
        home["source"]["inference"]["upstream_claim_ids"] = [away["claim_id"]]
        away["source"]["inference"]["upstream_claim_ids"] = [home["claim_id"]]
        with self.assertRaisesRegex(FootballResearchContractError, "cycle"):
            build_football_research_contract(cycle)

        after_cutoff = payload()
        after_cutoff["teams"]["home"]["tactical_context"]["source"]["inference"]["generated_at_utc"] = "2026-08-12T10:01:00Z"
        with self.assertRaisesRegex(FootballResearchContractError, "generated_at_utc"):
            build_football_research_contract(after_cutoff)

    def test_publication_discriminator_supports_null_not_published_and_observed_odds(self) -> None:
        contract = build_football_research_contract(payload())
        lineup_publication = contract["teams"]["home"]["availability"]["lineup"]["source"]["publication"]
        self.assertEqual(lineup_publication["state"], "not_published")
        self.assertIsNone(lineup_publication["published_at_utc"])
        odds_publication = contract["odds_proxies"][0]["source"]["publication"]
        self.assertEqual(odds_publication["state"], "observed")
        self.assertRegex(odds_publication["observed_at_utc"], r"Z$")
        self.assertRegex(contract["odds_proxies"][0]["source"]["retrieved_at_utc"], r"Z$")

        odds_published = payload()
        publication = odds_published["odds_proxies"][0]["source"]["publication"]
        publication.update({
            "state": "published",
            "published_at_utc": "2026-08-12T08:00:00Z",
            "observed_at_utc": None,
        })
        with self.assertRaisesRegex(FootballResearchContractError, "odds_proxy must be observed"):
            build_football_research_contract(odds_published)

        lineup_time = payload()
        lineup_time["teams"]["home"]["availability"]["lineup"]["source"]["publication"]["published_at_utc"] = "2026-08-12T08:00:00Z"
        with self.assertRaisesRegex(FootballResearchContractError, "timestamps must both be null"):
            build_football_research_contract(lineup_time)

    def test_safe_denials_chance_creation_and_chance_names_pass_but_forecast_fails(self) -> None:
        contract = build_football_research_contract(payload())
        notes = contract["teams"]["home"]["tactical_context"]["value"]
        self.assertIn("Chance creation is concentrated in wide areas.", notes)
        self.assertEqual(
            contract["teams"]["home"]["availability"]["injuries"]["value"]["entries"][0]["player_name"],
            "Chance Fixture Home",
        )

        for claim in (
            "Home 61% according to the model.",
            "The model assigns 61% chance of a home win.",
            "Home win probability is high.",
            "The win probability is 0.61.",
            "Model confidence is high.",
            "Brier score 0.18 was reported.",
        ):
            candidate = payload()
            candidate["teams"]["home"]["tactical_context"]["value"] = [claim]
            with self.subTest(claim=claim):
                with self.assertRaisesRegex(FootballResearchContractError, "future probability"):
                    build_football_research_contract(candidate)

    def test_prediction_metric_keys_and_boundary_overrides_fail_closed(self) -> None:
        for key in (
            "home_win_probability",
            "modelConfidence",
            "brier_score",
            "logLoss",
            "calibration_gap",
            "win_rate",
        ):
            candidate = payload()
            candidate["teams"]["home"]["availability"]["injuries"]["value"]["entries"][0][key] = 0.7
            with self.subTest(key=key):
                with self.assertRaisesRegex(FootballResearchContractError, "forbidden"):
                    build_football_research_contract(candidate)

        for field, value in (
            ("future_probability_available", True),
            ("probability_metrics_visible", True),
            ("odds_are_proxy_only", False),
            ("wallet_connection_allowed", True),
            ("order_placement_allowed", True),
            ("can_replace_user_decision", True),
        ):
            candidate = payload()
            candidate[field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(FootballResearchContractError, f"{field} is fixed"):
                    build_football_research_contract(candidate)

    def test_only_four_evidence_classes_and_field_specific_classes_are_allowed(self) -> None:
        self.assertEqual(
            FOOTBALL_EVIDENCE_CLASSES,
            frozenset({"official_fact", "media_report", "model_inference", "odds_proxy"}),
        )
        unknown = payload()
        unknown["teams"]["home"]["tactical_context"]["evidence_class"] = "rumor"
        with self.assertRaisesRegex(FootballResearchContractError, "one of"):
            build_football_research_contract(unknown)

        identity_inference = payload()
        field = identity_inference["match_identity"]["match_id"]
        field["evidence_class"] = "model_inference"
        field["source"] = source(
            "model_inference",
            source_id="made-up-match-id",
            upstream_claim_ids=[identity_inference["match_identity"]["competition"]["claim_id"]],
        )
        with self.assertRaisesRegex(FootballResearchContractError, "not allowed"):
            build_football_research_contract(identity_inference)

    def test_recursive_objects_are_closed_and_tampering_breaks_hash(self) -> None:
        mutations = []
        extra_source = payload()
        extra_source["teams"]["home"]["availability"]["injuries"]["source"]["license"] = "fixture"
        mutations.append(extra_source)
        extra_fixture = payload()
        extra_fixture["teams"]["home"]["schedule_context"]["fixture_history"]["value"][0]["score"] = "1-0"
        mutations.append(extra_fixture)
        extra_root = payload()
        extra_root["forecast"] = {}
        mutations.append(extra_root)
        for candidate in mutations:
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(FootballResearchContractError, "closed"):
                    build_football_research_contract(candidate)

        contract = build_football_research_contract(payload())
        tampered = copy.deepcopy(contract)
        tampered["teams"]["away"]["schedule_context"]["travel"]["value"]["distance_km"] = 999.0
        with self.assertRaisesRegex(FootballResearchContractError, "sha256 mismatch"):
            validate_football_research_contract(tampered)

    def test_canonical_utc_and_data_cutoff_fail_closed(self) -> None:
        non_utc = payload()
        non_utc["data_cutoff_utc"] = "2026-08-12T18:00:00+08:00"
        with self.assertRaisesRegex(FootballResearchContractError, "canonical UTC"):
            build_football_research_contract(non_utc)

        after_cutoff = payload()
        after_cutoff["teams"]["home"]["availability"]["lineup"]["source"]["retrieved_at_utc"] = "2026-08-12T10:01:00Z"
        with self.assertRaisesRegex(FootballResearchContractError, "data cutoff"):
            build_football_research_contract(after_cutoff)

        post_kickoff_cutoff = payload()
        post_kickoff_cutoff["data_cutoff_utc"] = KICKOFF
        with self.assertRaisesRegex(FootballResearchContractError, "before match kickoff"):
            build_football_research_contract(post_kickoff_cutoff)


if __name__ == "__main__":
    unittest.main()
