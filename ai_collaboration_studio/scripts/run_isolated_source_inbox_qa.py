from __future__ import annotations

"""Serve deterministic Source Inbox fixtures from a disposable local runtime.

The harness binds only an ephemeral loopback port, uses a system-temporary
SQLite database, and injects a fixed in-memory macro client.  It never starts a
monitoring worker, Provider, market connection, formal round, or external
network transport.
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FIXED_NOW_MS = 1_788_149_100_000


def _configure_isolation(temp_root: Path) -> Path:
    database_path = temp_root / "source-inbox-browser-qa.sqlite3"
    os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    os.environ["AI_STUDIO_RUNTIME_DIR"] = str(temp_root)
    os.environ["AI_STUDIO_DATABASE_PATH"] = str(database_path)
    os.environ["AI_STUDIO_HOST"] = "127.0.0.1"
    os.environ["AI_STUDIO_PORT"] = "0"
    os.environ["AI_STUDIO_SOURCE_MONITOR_ENABLED"] = "0"
    os.environ["AI_STUDIO_SOURCE_MONITOR_AUTO_START"] = "0"
    os.environ["AI_STUDIO_SOURCE_MONITOR_TRADING_IMPACT_RULES_ENABLED"] = "0"
    os.environ["FUTU_HOST"] = "127.0.0.1"
    os.environ["FUTU_PORT"] = "1"
    for name in (
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "ARK_API_KEY",
        "GLM_API_KEY",
        "ZHIPUAI_API_KEY",
    ):
        os.environ[name] = ""
    return database_path


def _manual_packet() -> dict[str, object]:
    return {
        "version": "source_import_packet_v1",
        "source_channel": "chatgpt_scheduled_task",
        "source_key": "github_ci_watch",
        "external_run_id": "phase6-browser-manual-run",
        "checked_at": "2026-08-28T13:03:00Z",
        "cutoff_at": "2026-08-28T13:00:00Z",
        "meaningful_change": True,
        "items": [{
            "version": "project_source_item_v1",
            "external_item_id": "phase6-browser-ci-event",
            "item_type": "ci_run_failure",
            "severity": "high",
            "occurred_at": "2026-08-28T12:55:00Z",
            "published_at": "2026-08-28T12:56:00Z",
            "entities": [{
                "kind": "repository",
                "id": "fixture/source-inbox",
                "label": "Source Inbox fixture",
            }],
            "headline": "隔离 CI 来源需要人工复核",
            "summary": "外部任务声明一次隔离测试失败；本地尚未核验。",
            "facts": [{
                "claim": "外部任务声明 workflow conclusion 为 failure。",
                "source_indexes": [0],
            }],
            "sources": [{
                "url": "https://github.com/example/source-inbox/actions/runs/100",
                "publisher": "GitHub",
                "source_type": "official_platform",
                "published_at": "2026-08-28T12:56:00Z",
                "content_sha256": "1" * 64,
            }],
            "impact_hypotheses": [],
            "unknowns": ["失败断言未导入。"],
            "confidence": 0.5,
            "recommended_route": "notify_only",
            "extensions": {"github_v1": {"run_status": "failure"}},
        }],
        "generation": {
            "channel": "chatgpt_scheduled_task",
            "model": "",
            "cost": {
                "status": "unavailable",
                "amount": None,
                "currency": "",
                "usage_source": "subscription_unavailable",
            },
            "correlated_output": True,
        },
    }


def _fed_release_row() -> dict[str, object]:
    return {
        "authority": "federal_reserve",
        "family": "monetary_policy",
        "reference_period": "2026-07-29",
        "official_id": "phase6-browser-fed-release",
        "title": "Federal Reserve policy release fixture",
        "summary": "Fixed official macro release used only for local browser QA.",
        "official_url": (
            "https://www.federalreserve.gov/newsevents/pressreleases/"
            "monetary20260729a.htm"
        ),
        "source_url": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "scheduled_at": "",
        "released_at": "2026-07-29T18:00:00Z",
        "official_revision": False,
        "data": {},
    }


class _FixedMacroClient:
    transport_identity = "phase6_browser_fixture_v1"
    source_manifest = {"version": "phase6_browser_fixture_manifest_v1"}

    def federal_reserve_releases(self, *, limit: int) -> dict[str, object]:
        if type(limit) is not int or limit < 1:
            raise AssertionError("fixture limit must be a positive native integer")
        return {"rows": [_fed_release_row()], "source_errors": []}


def _forbidden_counts(database_path: Path) -> dict[str, int]:
    tables = (
        "provider_execution_runs",
        "provider_call_attempts",
        "rounds",
        "source_inbox_round_drafts",
    )
    with closing(sqlite3.connect(database_path)) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve disposable Phase 6 Source Inbox browser fixtures.",
    )
    parser.add_argument("--lifetime-seconds", type=int, default=600)
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.lifetime_seconds <= 3600:
        parser.error("--lifetime-seconds must be between 1 and 3600")
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_args(argv)
    with tempfile.TemporaryDirectory(
        prefix="ai-studio-source-inbox-browser-qa-",
        ignore_cleanup_errors=True,
    ) as temp_dir:
        temp_root = Path(temp_dir).resolve()
        database_path = _configure_isolation(temp_root)

        from backend import http_server
        from backend.source_inbox_service import SourceInboxService
        from backend.source_monitoring.adapters.macro_official import (
            FederalReserveSourceAdapter,
        )
        from backend.source_monitoring.packet_builder import build_source_import_packet
        from backend.source_monitoring.trading_impact_rules import TradingImpactRulesV1
        from backend.store import StudioStore

        if not (PROJECT_ROOT / "frontend" / "dist" / "index.html").is_file():
            raise RuntimeError("frontend/dist is missing; run the production build first")

        store = StudioStore(database_path)
        room_id = store.create_room(
            "Phase 6 来源复核",
            "隔离浏览器 QA；不启动 Provider、市场或正式 round。",
            capability_pack_ids=[],
        )["room"]["id"]
        clock = [FIXED_NOW_MS]
        inbox = SourceInboxService(store, clock=lambda: clock[0] / 1_000)
        manual = inbox.import_packet(
            json.dumps(_manual_packet(), ensure_ascii=False),
        )["items"][0]

        adapter = FederalReserveSourceAdapter(client=_FixedMacroClient())
        poll = adapter.poll({}, observed_at_ms=clock[0], max_items=50)
        if poll.source_errors or len(poll.observed_items) != 1:
            raise RuntimeError("fixed macro fixture did not produce exactly one item")
        clock[0] += 1
        official_packet = build_source_import_packet(
            adapter_key=adapter.adapter_key,
            external_run_id="phase6-browser-federal-reserve-run",
            captured_at_ms=clock[0],
            observed_items=poll.observed_items,
            max_items=50,
        )
        official = inbox.import_packet(
            json.dumps(official_packet, ensure_ascii=False),
            actor="source_monitoring_worker",
            impact_rules=TradingImpactRulesV1(),
        )["items"][0]

        before = _forbidden_counts(database_path)
        http_server.STORE = store
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        if (
            server.server_port == 8770
            or server.server_port == 11111
            or server.server_port == 18787
        ):
            server.server_close()
            raise RuntimeError("ephemeral QA server selected a protected port")
        print(json.dumps({
            "url": f"http://127.0.0.1:{server.server_port}/",
            "room_id": room_id,
            "manual_item_id": manual["id"],
            "official_item_id": official["id"],
            "database_path": str(database_path),
            "formal_assets_used": False,
            "real_provider_calls_allowed": False,
            "market_connections_allowed": False,
            "maximum_lifetime_seconds": arguments.lifetime_seconds,
        }, ensure_ascii=False, sort_keys=True), flush=True)

        stop_watchdog = threading.Event()

        def shutdown_watchdog() -> None:
            if not stop_watchdog.wait(arguments.lifetime_seconds):
                server.shutdown()

        watchdog = threading.Thread(
            target=shutdown_watchdog,
            name="source-inbox-qa-shutdown",
            daemon=True,
        )
        watchdog.start()
        try:
            server.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass
        finally:
            stop_watchdog.set()
            server.server_close()
            watchdog.join(timeout=2)
            after = _forbidden_counts(database_path)
            print(json.dumps({
                "stopped": True,
                "forbidden_counts_before": before,
                "forbidden_counts_after": after,
                "forbidden_counts_unchanged": before == after,
            }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
