from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, time, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from backend import http_server
from backend.candidate_experiment import (
    CANDIDATE_EXPERIMENT_AUTHORIZATION_VERSION,
    CANDIDATE_EXPERIMENT_COHORT_VERSION,
    CANDIDATE_EXPERIMENT_REQUEST_VERSION,
    CandidateExperimentError,
    CandidateExperimentService,
)
from backend.decision_lineage import canonical_sha256
from backend.store import StudioStore


ROOM_ID = "room_storage"
STORAGE_SYMBOLS = ("US.MU", "US.SNDK", "US.WDC", "US.STX")
ATTESTATION_SHA256 = "a" * 64
PROJECTION_SHA256 = "b" * 64
SAFETY_FIELDS = {
    "execution_capability": "none",
    "live_trading_allowed": False,
    "can_autonomously_decide": False,
    "ranking_produced": False,
    "winner_claim": False,
    "user_final_decision_required": True,
}


def fake_history(symbol: str, *, daily_return: float) -> dict:
    first_day = date(2025, 1, 2)
    close = 100.0
    rows = []
    for index in range(500):
        market_day = first_day + timedelta(days=index)
        open_price = close
        if index:
            close *= 1 + daily_return
        close_price = round(close, 8)
        open_price = round(open_price, 8)
        rows.append({
            "symbol": symbol,
            "market_time": f"{market_day.isoformat()} 16:00:00",
            "time": datetime.combine(
                market_day,
                time(16),
                tzinfo=ZoneInfo("America/New_York"),
            ).astimezone(timezone.utc).isoformat(),
            "open": open_price,
            "high": round(max(open_price, close_price) * 1.01, 8),
            "low": round(min(open_price, close_price) * 0.99, 8),
            "close": close_price,
            "volume": 50_000_000.0,
            "turnover": round(close_price * 50_000_000.0, 8),
        })
    last_day = first_day + timedelta(days=len(rows) - 1)
    return {
        "ok": True,
        "source": "futu_opend",
        "interval": "1d",
        "price_adjustment": "QFQ",
        "captured_at": "2026-07-30T20:00:00.000Z",
        "as_of_date": "2026-07-30",
        "last_completed_session": last_day.isoformat(),
        "actual_start": first_day.isoformat(),
        "actual_end": last_day.isoformat(),
        "symbol": symbol,
        "rows": rows,
        "source_errors": [],
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


class FakeBatchMarket:
    """One explicitly fake, read-only batch source; no Futu process is touched."""

    def __init__(self) -> None:
        self.calls = 0
        self.requested_symbols: tuple[str, ...] = ()

    def history_batch(self, symbols, **_kwargs) -> dict:
        self.calls += 1
        self.requested_symbols = tuple(symbols)
        histories = {
            symbol: fake_history(
                symbol,
                daily_return=0.0003 * (index + 1),
            )
            for index, symbol in enumerate(self.requested_symbols)
        }
        return {
            "ok": True,
            "source": "futu_opend",
            "interval": "1d",
            "price_adjustment": "QFQ",
            "captured_at": "2026-07-30T20:00:00.000Z",
            "as_of_date": "2026-07-30",
            "symbols": list(self.requested_symbols),
            "histories": histories,
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


class ForbiddenProviders:
    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str):
        self.calls += 1
        raise AssertionError(f"provider dependency must not be used: {name}")


def recursive_keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys.update(recursive_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(recursive_keys(child))
    return keys


class CandidateExperimentHttpTests(unittest.TestCase):
    """HTTP contract tests with a temporary DB and deterministic fake market."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="p23-http-")
        self.store = StudioStore(Path(self.temp_dir.name) / "candidate-http.sqlite3")
        self.artifact = self.store.create_artifact(
            ROOM_ID,
            title="P23 HTTP isolated artifact",
            content={
                "summary": "Only an isolated HTTP fixture.",
                "summary_evidence": [],
                "requirements": [],
                "risks": [],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {
                    "status": "undecided",
                    "options": [],
                    "preferred_option_id": "",
                    "rationale": "",
                    "evidence": [],
                },
            },
            created_by="offline_http_test",
        )
        self.candidate_fixtures = {
            "candidate_a": self._candidate_fixture(
                "candidate_a",
                symbol="US.MU",
                direction="UP",
                title="Candidate A",
            ),
            "candidate_b": self._candidate_fixture(
                "candidate_b",
                symbol="US.WDC",
                direction="DOWN",
                title="Candidate B",
            ),
        }
        self.market = FakeBatchMarket()
        self.providers = ForbiddenProviders()
        self.original_store = http_server.STORE
        self.original_market = http_server.STORAGE_MARKET
        self.original_providers = http_server.PROVIDERS
        http_server.STORE = self.store
        http_server.STORAGE_MARKET = self.market
        http_server.PROVIDERS = self.providers

        def fake_authorization_context(
            _service,
            _connection,
            room_id,
            request,
            *,
            require_current,
        ):
            return self._authorization_context(
                room_id,
                request,
                require_current=require_current,
            )

        self.context_patch = patch.object(
            CandidateExperimentService,
            "_authorization_context",
            autospec=True,
            side_effect=fake_authorization_context,
        )
        self.context_patch.start()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.context_patch.stop()
        http_server.STORE = self.original_store
        http_server.STORAGE_MARKET = self.original_market
        http_server.PROVIDERS = self.original_providers
        self.temp_dir.cleanup()

    @staticmethod
    def _candidate_fixture(
        candidate_id: str,
        *,
        symbol: str,
        direction: str,
        title: str,
    ) -> dict:
        snapshot = {
            "id": candidate_id,
            "title": title,
            "symbol": symbol,
            "direction": direction,
            "horizon_days": 20,
            "thesis": f"Historical-only thesis for {title}.",
            "invalidation": f"Invalidate {title} when its premise changes.",
        }
        return {
            "candidate_id": candidate_id,
            "revision": 1,
            "origin_message_id": f"message_{candidate_id}_origin",
            "latest_message_id": f"message_{candidate_id}_risk_review",
            "snapshot": snapshot,
            "snapshot_sha256": canonical_sha256(snapshot),
        }

    def _authorization_context(
        self,
        room_id: str,
        request: dict,
        *,
        require_current: bool,
    ) -> dict:
        if room_id != ROOM_ID:
            raise CandidateExperimentError(
                "CANDIDATE_EXPERIMENT_ROOM_NOT_FOUND",
                "isolated room not found",
                status=404,
            )
        if (
            request.get("artifact_id") != self.artifact["id"]
            or request.get("expected_artifact_version") != self.artifact["version"]
        ):
            raise CandidateExperimentError(
                "CANDIDATE_EXPERIMENT_ARTIFACT_VERSION_DRIFT",
                "isolated artifact binding changed",
                status=409,
            )
        candidates = []
        for selection in request.get("candidate_selections") or []:
            fixture = self.candidate_fixtures.get(selection.get("candidate_id"))
            if fixture is None:
                raise CandidateExperimentError(
                    "CANDIDATE_EXPERIMENT_CANDIDATE_NOT_UNIQUE",
                    "isolated candidate not found",
                    status=409,
                )
            expected = {
                "expected_candidate_revision": fixture["revision"],
                "expected_candidate_origin_message_id": fixture["origin_message_id"],
                "expected_candidate_latest_message_id": fixture["latest_message_id"],
                "expected_candidate_snapshot_sha256": fixture["snapshot_sha256"],
            }
            if any(selection.get(key) != value for key, value in expected.items()):
                raise CandidateExperimentError(
                    "CANDIDATE_EXPERIMENT_CANDIDATE_VERSION_DRIFT",
                    "isolated candidate binding changed",
                    status=409,
                )
            option = {
                "id": fixture["candidate_id"],
                "title": fixture["snapshot"]["title"],
                "evidence": [{
                    "id": f"evidence_{fixture['candidate_id']}",
                    "label": "offline evidence",
                }],
                "risks": [{
                    "id": f"counter_{fixture['candidate_id']}",
                    "label": "offline counterevidence",
                }],
            }
            candidate = {
                "candidate_id": fixture["candidate_id"],
                "candidate_revision": fixture["revision"],
                "candidate_origin_message_id": fixture["origin_message_id"],
                "candidate_latest_message_id": fixture["latest_message_id"],
                "candidate_snapshot": copy.deepcopy(fixture["snapshot"]),
                "candidate_snapshot_sha256": fixture["snapshot_sha256"],
                "artifact_option_snapshot": option,
                "artifact_option_snapshot_sha256": canonical_sha256(option),
                "title": fixture["snapshot"]["title"],
                "symbol": fixture["snapshot"]["symbol"],
                "direction": fixture["snapshot"]["direction"],
                "side": "LONG" if fixture["snapshot"]["direction"] == "UP" else "SHORT",
                "horizon_days": 20,
                "thesis": fixture["snapshot"]["thesis"],
                "invalidation": fixture["snapshot"]["invalidation"],
                "evidence": copy.deepcopy(option["evidence"]),
                "counterevidence": copy.deepcopy(option["risks"]),
                "risk_review": {
                    "action": "challenge",
                    "review_message_id": fixture["latest_message_id"],
                    "reviewer_member_id": "offline_risk_reviewer",
                    "reviewer_member_version": 1,
                    "risk_ids": [f"counter_{fixture['candidate_id']}"],
                    "disposition_only": True,
                },
                **SAFETY_FIELDS,
            }
            candidate["candidate_binding_sha256"] = canonical_sha256(candidate)
            candidates.append(candidate)
        artifact_snapshot_sha256 = canonical_sha256({
            "artifact_id": self.artifact["id"],
            "artifact_version": self.artifact["version"],
        })
        binding = {
            "version": "candidate_experiment_authorization_binding_v1",
            "room_id": ROOM_ID,
            "artifact_id": self.artifact["id"],
            "artifact_version": self.artifact["version"],
            "artifact_snapshot_sha256": artifact_snapshot_sha256,
            "governance_attestation_sha256": ATTESTATION_SHA256,
            "governance_projection_sha256": PROJECTION_SHA256,
            "candidate_bindings": copy.deepcopy(candidates),
            "common_horizon_days": 20,
            "invalidation_conditions": [
                "artifact_exact_version_or_confirmation_changes_before_commit",
                "governance_attestation_or_projection_changes_before_commit",
                "candidate_revision_origin_latest_or_snapshot_changes_before_commit",
                "server_common_spec_or_dataset_seal_mismatch",
                "any_arm_or_aggregate_integrity_failure",
            ],
            "does_not_imply_artifact_support": True,
            "does_not_create_artifact_user_decision": True,
            **SAFETY_FIELDS,
        }
        return {
            "artifact": copy.deepcopy(self.artifact),
            "source_current": True,
            "governance": {"projection_sha256": PROJECTION_SHA256},
            "candidates": candidates,
            "common_horizon_days": 20,
            "artifact_snapshot_sha256": artifact_snapshot_sha256,
            "authorization_binding": binding,
            "authorization_binding_sha256": canonical_sha256(binding),
        }

    def request_payload(self) -> dict:
        selections = []
        for candidate_id in ("candidate_a", "candidate_b"):
            fixture = self.candidate_fixtures[candidate_id]
            selections.append({
                "candidate_id": candidate_id,
                "expected_candidate_revision": fixture["revision"],
                "expected_candidate_origin_message_id": fixture["origin_message_id"],
                "expected_candidate_latest_message_id": fixture["latest_message_id"],
                "expected_candidate_snapshot_sha256": fixture["snapshot_sha256"],
            })
        return {
            "version": CANDIDATE_EXPERIMENT_REQUEST_VERSION,
            "client_request_id": "candidate_experiment_http_request_0001",
            "artifact_id": self.artifact["id"],
            "expected_artifact_version": self.artifact["version"],
            "expected_governance_attestation_sha256": ATTESTATION_SHA256,
            "candidate_selections": selections,
            "user_authorized_historical_comparison": True,
        }

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> tuple[int, dict]:
        headers = {}
        data = None
        if payload is not None:
            headers = {
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            }
            data = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            method=method,
            headers=headers,
            data=data,
        )
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def assert_safety_fields(self, value: dict) -> None:
        for key, expected in SAFETY_FIELDS.items():
            self.assertEqual(value.get(key), expected, key)

    def test_create_reread_idempotency_conflict_and_public_projection(self) -> None:
        request_payload = self.request_payload()
        path = f"/api/rooms/{ROOM_ID}/candidate-experiments"

        status, created_payload = self.request_json("POST", path, request_payload)
        self.assertEqual(status, 201, created_payload)
        self.assertTrue(created_payload["ok"])
        created = created_payload["experiment"]
        self.assertEqual(created["version"], CANDIDATE_EXPERIMENT_COHORT_VERSION)
        self.assertEqual(
            created["authorization"]["version"],
            CANDIDATE_EXPERIMENT_AUTHORIZATION_VERSION,
        )
        self.assertFalse(created["idempotent_replay"])
        self.assertTrue(created["integrity_ok"])
        self.assertTrue(created["metrics_visible"])
        self.assertEqual(len(created["arms"]), 2)
        self.assertEqual(self.market.calls, 1)
        self.assertEqual(self.market.requested_symbols, STORAGE_SYMBOLS)
        self.assertEqual(self.providers.calls, 0)
        self.assert_safety_fields(created)
        for arm in created["arms"]:
            self.assert_safety_fields(arm)
            self.assertEqual(arm["shared_spec_sha256"], created["spec_sha256"])
            self.assertEqual(
                arm["shared_dataset_seal_sha256"],
                created["dataset_seal_sha256"],
            )

        status, reread_payload = self.request_json(
            "GET",
            f"{path}/{created['id']}",
        )
        self.assertEqual(status, 200)
        self.assertTrue(reread_payload["ok"])
        expected_reread = copy.deepcopy(created)
        expected_reread.pop("idempotent_replay")
        self.assertEqual(reread_payload["experiment"], expected_reread)
        self.assertEqual(self.market.calls, 1)
        self.assertEqual(self.providers.calls, 0)

        status, replay_payload = self.request_json("POST", path, request_payload)
        self.assertEqual(status, 200)
        self.assertTrue(replay_payload["ok"])
        self.assertTrue(replay_payload["experiment"]["idempotent_replay"])
        self.assertEqual(replay_payload["experiment"]["id"], created["id"])
        self.assertEqual(self.market.calls, 1)
        self.assertEqual(self.providers.calls, 0)

        changed_payload = copy.deepcopy(request_payload)
        changed_payload["candidate_selections"].reverse()
        status, conflict = self.request_json("POST", path, changed_payload)
        self.assertEqual(status, 409)
        self.assertFalse(conflict["ok"])
        self.assertEqual(
            conflict["code"],
            "CANDIDATE_EXPERIMENT_IDEMPOTENCY_CONFLICT",
        )
        self.assertEqual(self.market.calls, 1)
        self.assertEqual(self.providers.calls, 0)

        exposed_keys = recursive_keys(created_payload)
        self.assertTrue({
            "histories",
            "rows",
            "plan",
            "result",
            "request_semantics",
            "authorization_binding",
            "candidate_snapshot",
            "artifact_option_snapshot",
            "risk_review",
            "input_seal",
        }.isdisjoint(exposed_keys), exposed_keys)

    def test_typed_http_errors_preserve_status_and_code_without_dependencies(self) -> None:
        invalid = self.request_payload()
        invalid["client_supplied_cutoff"] = "2025-12-31"
        path = f"/api/rooms/{ROOM_ID}/candidate-experiments"

        status, bad_request = self.request_json("POST", path, invalid)
        self.assertEqual(status, 400)
        self.assertFalse(bad_request["ok"])
        self.assertEqual(
            bad_request["code"],
            "CANDIDATE_EXPERIMENT_REQUEST_INVALID",
        )

        status, missing = self.request_json(
            "GET",
            f"{path}/candidate_experiment_cohort_missing",
        )
        self.assertEqual(status, 404)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "CANDIDATE_EXPERIMENT_NOT_FOUND")
        self.assertEqual(self.market.calls, 0)
        self.assertEqual(self.providers.calls, 0)


if __name__ == "__main__":
    unittest.main()
