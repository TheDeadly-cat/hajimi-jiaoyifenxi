from __future__ import annotations

"""Serve a disposable P23 browser-QA fixture without live dependencies.

This is deliberately a test harness, not an application launcher.  It sets
all isolation variables before importing the backend, creates a temporary
SQLite database, injects deterministic fake market/readiness/provider
dependencies, and asks Windows for an ephemeral loopback port.  The temporary
database is deleted when the process exits.
"""

import copy
import json
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


ROOM_TITLE = "P23 isolated browser QA"
ATTESTATION_SHA256 = "a" * 64
PROJECTION_SHA256 = "b" * 64
STORAGE_SYMBOLS = ("US.MU", "US.SNDK", "US.WDC", "US.STX")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAFETY_FIELDS = {
    "execution_capability": "none",
    "live_trading_allowed": False,
    "can_autonomously_decide": False,
    "ranking_produced": False,
    "winner_claim": False,
    "user_final_decision_required": True,
}


def _configure_isolation(temp_root: Path) -> Path:
    database_path = temp_root / "candidate-experiment-browser-qa.sqlite3"
    os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    os.environ["AI_STUDIO_RUNTIME_DIR"] = str(temp_root)
    os.environ["AI_STUDIO_DATABASE_PATH"] = str(database_path)
    os.environ["AI_STUDIO_HOST"] = "127.0.0.1"
    os.environ["AI_STUDIO_PORT"] = "0"
    os.environ["FUTU_HOST"] = "127.0.0.1"
    os.environ["FUTU_PORT"] = "1"
    os.environ["SEC_USER_AGENT"] = ""
    # Never inherit or inspect a real credential in this process.
    for name in (
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "ARK_API_KEY",
        "GLM_API_KEY",
        "ZHIPUAI_API_KEY",
    ):
        os.environ[name] = ""
    return database_path


def _write_ready_file(payload: dict[str, Any]) -> None:
    raw_path = os.environ.get("AI_STUDIO_QA_READY_FILE", "").strip()
    if not raw_path:
        return
    ready_path = Path(raw_path).resolve()
    temp_parent = Path(tempfile.gettempdir()).resolve()
    try:
        ready_path.relative_to(temp_parent)
    except ValueError as exc:
        raise RuntimeError("AI_STUDIO_QA_READY_FILE must stay under the system temp directory") from exc
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    ready_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _fake_history(symbol: str, *, daily_return: float) -> dict[str, Any]:
    first_day = date(2025, 1, 2)
    close = 100.0
    rows: list[dict[str, Any]] = []
    # walk-forward v3 needs 20 non-overlapping 20-day test windows after the
    # fixed 99-day train segment; 520 rows keep the browser run realistic.
    for index in range(520):
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
        # The P23 engine contract names this source; this object is still an
        # in-memory fixture and never imports or connects to Futu/OpenD.
        "source": "futu_opend",
        "interval": "1d",
        "price_adjustment": "QFQ",
        "captured_at": "2027-01-02T20:00:00.000Z",
        "as_of_date": "2027-01-02",
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
    """Deterministic read-only market fixture with one observable batch read."""

    def __init__(self) -> None:
        self.calls = 0
        self.requested_symbols: tuple[str, ...] = ()

    def history_batch(self, symbols: Any, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.requested_symbols = tuple(str(symbol) for symbol in symbols)
        return {
            "ok": True,
            "source": "futu_opend",
            "interval": "1d",
            "price_adjustment": "QFQ",
            "captured_at": "2027-01-02T20:00:00.000Z",
            "as_of_date": "2027-01-02",
            "symbols": list(self.requested_symbols),
            "histories": {
                symbol: _fake_history(
                    symbol,
                    daily_return=0.0003 * (index + 1),
                )
                for index, symbol in enumerate(self.requested_symbols)
            },
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    @staticmethod
    def status() -> dict[str, Any]:
        return {
            "connected": False,
            "configured": False,
            "mode": "isolated_fake_market",
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    @staticmethod
    def snapshot(*, force: bool = False) -> dict[str, Any]:
        del force
        return {
            "ok": False,
            "source": "isolated_fake_market",
            "symbols": list(STORAGE_SYMBOLS),
            "source_errors": ["browser QA uses the frozen fake history batch only"],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected market dependency access: {name}")


class FakeReadiness:
    @staticmethod
    def inspect(**_kwargs: Any) -> dict[str, Any]:
        return {
            "version": "storage_research_readiness_v1",
            "required": True,
            "ready": False,
            "status": "isolated_fake_market",
            "issues": [{
                "code": "ISOLATED_BROWSER_QA",
                "message": "Only the deterministic experiment history fixture is enabled.",
            }],
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        }


class ForbiddenProviders:
    """Provider registry stand-in: metadata is empty and calls fail closed."""

    def __init__(self) -> None:
        self.call_attempts = 0

    @staticmethod
    def status() -> list[dict[str, Any]]:
        return []

    def __getattr__(self, name: str) -> Any:
        self.call_attempts += 1
        raise AssertionError(f"provider dependency must not be used: {name}")


def _candidate_fixture(
    canonical_sha256: Any,
    candidate_id: str,
    *,
    symbol: str,
    direction: str,
    title: str,
) -> dict[str, Any]:
    snapshot = {
        "id": candidate_id,
        "title": title,
        "symbol": symbol,
        "direction": direction,
        "horizon_days": 20,
        "thesis": f"Historical-only thesis for {title}.",
        "invalidation": f"Invalidate {title} when its frozen premise changes.",
    }
    return {
        "candidate_id": candidate_id,
        "revision": 1,
        "origin_message_id": f"message_{candidate_id}_origin",
        "latest_message_id": f"message_{candidate_id}_risk_review",
        "snapshot": snapshot,
        "snapshot_sha256": canonical_sha256(snapshot),
    }


def _governed_artifact(
    artifact: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    options = [{
        "id": fixture["candidate_id"],
        "title": fixture["snapshot"]["title"],
        "description": f"Frozen option for {fixture['snapshot']['title']}.",
        "lineage": {
            "version": "candidate_lineage_v1",
            "revision": fixture["revision"],
            "origin_message_id": fixture["origin_message_id"],
            "latest_message_id": fixture["latest_message_id"],
        },
    } for fixture in candidates.values()]
    candidate_lineage = [{
        "id": fixture["candidate_id"],
        "revision": fixture["revision"],
        "origin_message_id": fixture["origin_message_id"],
        "latest_message_id": fixture["latest_message_id"],
    } for fixture in candidates.values()]
    reviews = [{
        "candidate_id": fixture["candidate_id"],
        "candidate_revision": fixture["revision"],
        "current_candidate_revision": fixture["revision"],
        "candidate_latest_message_id": fixture["latest_message_id"],
        "candidate_snapshot": copy.deepcopy(fixture["snapshot"]),
        "candidate_snapshot_sha256": fixture["snapshot_sha256"],
        "action": "challenge",
        "status": "current",
        "review_message_id": f"review_{fixture['candidate_id']}",
        "reviewer_member_id": "offline_risk_reviewer",
        "reviewer_member_version": 1,
    } for fixture in candidates.values()]
    projection = {
        "version": "turn_contract_v1",
        "decision": {
            "status": "candidate",
            "preferred_option_id": "candidate_a",
            "options": options,
        },
        "candidate_lineage": {
            "version": "candidate_lineage_v1",
            "applicable": True,
            "ready": True,
            "status": "ready",
            "candidates": candidate_lineage,
            "issues": [],
        },
        "candidate_risk_reviews": {
            "version": "candidate_risk_review_v1",
            "applicable": True,
            "ready": True,
            "status": "ready",
            "target_candidate_count": len(reviews),
            "reviewed_candidate_count": len(reviews),
            "current_review_count": len(reviews),
            "stale_review_count": 0,
            "reviews": reviews,
            "issues": [],
            "review_actions_are_dispositions_only": True,
            **SAFETY_FIELDS,
        },
        **SAFETY_FIELDS,
    }
    governed = copy.deepcopy(artifact)
    governed.update({
        "status": "CONFIRMED",
        "evidence_review": {"confirmation_ready": True},
        "content": {
            **copy.deepcopy(artifact.get("content") or {}),
            "decision": copy.deepcopy(projection["decision"]),
        },
        "governance_snapshot": {
            "version": "artifact_governance_v1",
            "applicable": True,
            "ready": True,
            "status": "ready",
            "integrity_ok": True,
            "attestation_integrity_ok": True,
            "attestation_sha256": ATTESTATION_SHA256,
            "artifact": {
                "artifact_id": artifact["id"],
                "artifact_version": artifact["version"],
            },
            "artifact_alignment": {"ready": True, "integrity_ok": True},
            "projection": projection,
            **SAFETY_FIELDS,
        },
    })
    return governed


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ai-studio-p23-browser-qa-") as raw_temp:
        temp_root = Path(raw_temp).resolve()
        database_path = _configure_isolation(temp_root).resolve()
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))

        # Backend imports must remain below the isolation setup above.
        from backend import http_server
        from backend.candidate_experiment import (
            CandidateExperimentError,
            CandidateExperimentService,
        )
        from backend.decision_lineage import canonical_sha256
        from backend.store import StudioStore

        if database_path.parent != temp_root:
            raise RuntimeError("QA database escaped its temporary runtime")
        if not http_server.FRONTEND_DIST.joinpath("index.html").is_file():
            raise RuntimeError(
                "frontend/dist is missing; build the frontend before browser QA"
            )

        store = StudioStore(database_path)
        room_snapshot = store.create_room(
            ROOM_TITLE,
            "Visual and interaction QA for the P23 atomic historical cohort.",
            template_id="open_collaboration",
            capability_pack_ids=["storage_research_readonly"],
        )
        room_id = str((room_snapshot.get("room") or {}).get("id") or "")
        artifact = store.create_artifact(
            room_id,
            title="P23 exact governed candidate cohort",
            content={
                "summary": "Disposable browser-QA artifact; no live data or Provider.",
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
            created_by="isolated_browser_qa",
        )
        if not artifact:
            raise RuntimeError("failed to seed the disposable QA artifact")

        candidates = {
            "candidate_a": _candidate_fixture(
                canonical_sha256,
                "candidate_a",
                symbol="US.MU",
                direction="UP",
                title="MU historical thesis",
            ),
            "candidate_b": _candidate_fixture(
                canonical_sha256,
                "candidate_b",
                symbol="US.WDC",
                direction="DOWN",
                title="WDC counter thesis",
            ),
            "candidate_c": _candidate_fixture(
                canonical_sha256,
                "candidate_c",
                symbol="US.STX",
                direction="UP",
                title="STX alternative thesis",
            ),
        }
        governed_artifact = _governed_artifact(artifact, candidates)
        original_room_snapshot = store.room_snapshot

        def qa_room_snapshot(target_room_id: str) -> dict[str, Any] | None:
            snapshot = original_room_snapshot(target_room_id)
            if snapshot and target_room_id == room_id:
                snapshot["artifacts"] = [
                    copy.deepcopy(governed_artifact)
                    if item.get("id") == artifact["id"]
                    else item
                    for item in snapshot.get("artifacts") or []
                ]
            return snapshot

        # Instance override is intentionally limited to this disposable store.
        store.room_snapshot = qa_room_snapshot  # type: ignore[method-assign]

        def authorization_context(
            _service: Any,
            _connection: Any,
            target_room_id: str,
            request: dict[str, Any],
            *,
            require_current: bool,
        ) -> dict[str, Any]:
            del require_current
            if target_room_id != room_id:
                raise CandidateExperimentError(
                    "CANDIDATE_EXPERIMENT_ROOM_NOT_FOUND",
                    "isolated QA room not found",
                    status=404,
                )
            if (
                request.get("artifact_id") != artifact["id"]
                or request.get("expected_artifact_version") != artifact["version"]
                or request.get("expected_governance_attestation_sha256")
                != ATTESTATION_SHA256
            ):
                raise CandidateExperimentError(
                    "CANDIDATE_EXPERIMENT_ARTIFACT_VERSION_DRIFT",
                    "isolated artifact binding changed",
                    status=409,
                )
            bound_candidates: list[dict[str, Any]] = []
            for selection in request.get("candidate_selections") or []:
                fixture = candidates.get(str(selection.get("candidate_id") or ""))
                if fixture is None:
                    raise CandidateExperimentError(
                        "CANDIDATE_EXPERIMENT_CANDIDATE_NOT_UNIQUE",
                        "isolated candidate not found",
                        status=409,
                    )
                expected = {
                    "expected_candidate_revision": fixture["revision"],
                    "expected_candidate_origin_message_id": fixture[
                        "origin_message_id"
                    ],
                    "expected_candidate_latest_message_id": fixture[
                        "latest_message_id"
                    ],
                    "expected_candidate_snapshot_sha256": fixture[
                        "snapshot_sha256"
                    ],
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
                        "label": "offline supporting evidence",
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
                    "side": (
                        "LONG" if fixture["snapshot"]["direction"] == "UP"
                        else "SHORT"
                    ),
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
                bound_candidates.append(candidate)

            artifact_snapshot_sha256 = canonical_sha256({
                "artifact_id": artifact["id"],
                "artifact_version": artifact["version"],
            })
            binding = {
                "version": "candidate_experiment_authorization_binding_v1",
                "room_id": room_id,
                "artifact_id": artifact["id"],
                "artifact_version": artifact["version"],
                "artifact_snapshot_sha256": artifact_snapshot_sha256,
                "governance_attestation_sha256": ATTESTATION_SHA256,
                "governance_projection_sha256": PROJECTION_SHA256,
                "candidate_bindings": copy.deepcopy(bound_candidates),
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
                "artifact": copy.deepcopy(governed_artifact),
                "source_current": True,
                "governance": {"projection_sha256": PROJECTION_SHA256},
                "candidates": bound_candidates,
                "common_horizon_days": 20,
                "artifact_snapshot_sha256": artifact_snapshot_sha256,
                "authorization_binding": binding,
                "authorization_binding_sha256": canonical_sha256(binding),
            }

        fake_market = FakeBatchMarket()
        fake_providers = ForbiddenProviders()
        http_server.STORE = store
        http_server.STORAGE_MARKET = fake_market
        http_server.STORAGE_READINESS = FakeReadiness()
        http_server.PROVIDERS = fake_providers

        qa_state = {
            "temporary_database": True,
            "fake_market": True,
            "futu_or_opend_connected": False,
            "room_id": room_id,
            "artifact_id": artifact["id"],
        }

        class QaRequestHandler(http_server.StudioRequestHandler):
            def do_GET(self) -> None:
                if urlparse(self.path).path == "/__qa/status":
                    if not self._guard_request():
                        return
                    self._send_json({
                        "ok": True,
                        **qa_state,
                        "market_data_reads": fake_market.calls,
                        "market_symbols": list(fake_market.requested_symbols),
                        "provider_call_attempts": fake_providers.call_attempts,
                    })
                    return
                super().do_GET()

        with patch.object(
            CandidateExperimentService,
            "_authorization_context",
            new=authorization_context,
        ):
            server = ThreadingHTTPServer(("127.0.0.1", 0), QaRequestHandler)
            if server.server_port == 8770:
                server.server_close()
                raise RuntimeError("ephemeral QA server must never use port 8770")
            qa_state["port"] = server.server_port
            qa_state["formal_port_8770_used"] = False
            url = f"http://127.0.0.1:{server.server_port}/"
            ready_payload = {
                "ok": True,
                "url": url,
                "qa_status_url": f"{url}__qa/status",
                **qa_state,
            }
            _write_ready_file(ready_payload)
            print(json.dumps(ready_payload, ensure_ascii=False), flush=True)
            print("Press Ctrl+C to stop; the temporary database will be deleted.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()

        if fake_providers.call_attempts:
            raise RuntimeError("a Provider dependency was accessed during QA")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
