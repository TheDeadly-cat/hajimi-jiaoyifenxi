from __future__ import annotations

"""Serve a disposable Manual ChatGPT browser-QA fixture.

The harness is intentionally unable to reach a real Provider or formal
database.  It creates one temporary room at an explicitly selected workflow
state, injects a deterministic fake Provider, and binds the normal host to an
ephemeral 127.0.0.1 port for browser interaction checks.  A valid import
fixture is written only inside the temporary runtime directory so browser QA
can exercise the real clipboard/import UI without adding a product endpoint.
"""

import copy
import argparse
import json
import os
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


INITIAL_STATES = (
    "bundle-ready",
    "waiting",
    "import-rejected",
    "api-review",
)


def _configure_isolation(temp_root: Path) -> Path:
    database_path = temp_root / "manual-chatgpt-browser-qa.sqlite3"
    os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    os.environ["AI_STUDIO_RUNTIME_DIR"] = str(temp_root)
    os.environ["AI_STUDIO_DATABASE_PATH"] = str(database_path)
    os.environ["AI_STUDIO_HOST"] = "127.0.0.1"
    os.environ["AI_STUDIO_PORT"] = "0"
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


class FakeReviewProvider:
    provider_id = "fake-review"

    def __init__(self, response_type: Any, review_version: str) -> None:
        self.calls = 0
        self.response_type = response_type
        self.review_version = review_version

    def status(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": "Isolated Fake Review",
            "configured": True,
            "model": "fake-review-v1",
            "api": "in-memory fixture",
        }

    def probe(self, *, model: str = "") -> Any:
        del model
        raise AssertionError("browser QA review must not perform probe calls")

    def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> Any:
        del instructions
        request = json.loads(input_text)
        self.calls += 1
        review = {
            "version": self.review_version,
            "review_kind": request["review_kind"],
            "verdict": "pass",
            "summary": f"隔离审查 {request['review_kind']} 已完成。",
            "findings": [],
            "open_questions": [],
        }
        return self.response_type(
            ok=True,
            content=json.dumps(review, ensure_ascii=False),
            provider=self.provider_id,
            model=model,
            usage={"input_tokens": 100, "output_tokens": 20},
        )


def _valid_import(session: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(session["import_contract"]["result_template"])
    result["declared_model"] = "browser-qa-user-declaration"
    for panel in result["panels"]:
        panel["summary"] = f"{panel['panel_kind']} 隔离摘要"
        panel["conclusion"] = f"{panel['panel_kind']} 隔离结论"
        panel["disagreements"] = ["保留一个可见分歧。"]
        panel["risks"] = ["保留研究只读边界。"]
        for role in panel["role_views"]:
            role["assessment"] = f"{role['role_id']} 的有界评估。"
            role["uncertainty"] = "该内容仅用于浏览器 QA。"
    synthesis = result["final_synthesis"]
    synthesis["summary"] = "隔离决定摘要；等待独立 API 审查。"
    synthesis["decision_options"][0]["title"] = "保留研究只读方案"
    synthesis["decision_options"][0]["rationale"] = "不授予交易或外部执行权限。"
    synthesis["recommended_option_id"] = "option_1"
    synthesis["open_questions"] = ["用户是否确认冻结？"]
    return result


def _invalid_import(session: dict[str, Any]) -> dict[str, Any]:
    result = _valid_import(session)
    result["panels"][0]["conclusion"] = ""
    return result


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve a disposable Manual ChatGPT browser-QA fixture.",
    )
    parser.add_argument(
        "--lifetime-seconds",
        type=int,
        default=600,
        help="Maximum server lifetime before automatic shutdown (1-3600 seconds).",
    )
    parser.add_argument(
        "--keep-open-after-frozen",
        action="store_true",
        help="Keep serving after the fixture reaches FROZEN, until the lifetime expires.",
    )
    parser.add_argument(
        "--initial-state",
        choices=INITIAL_STATES,
        default="api-review",
        help=(
            "Workflow state exposed at startup: bundle-ready, waiting, "
            "import-rejected, or api-review (default)."
        ),
    )
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.lifetime_seconds <= 3600:
        parser.error("--lifetime-seconds must be between 1 and 3600")
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_args(argv)
    with tempfile.TemporaryDirectory(
        prefix="ai-studio-manual-chatgpt-browser-qa-",
        ignore_cleanup_errors=True,
    ) as temp_dir:
        temp_root = Path(temp_dir).resolve()
        database_path = _configure_isolation(temp_root)

        from backend import http_server
        from backend.manual_chatgpt import (
            MANUAL_CHATGPT_API_REVIEW_VERSION,
            ManualChatGPTService,
        )
        from backend.providers.base import ProviderResponse
        from backend.providers.registry import ProviderRegistry
        from backend.store import StudioStore

        store = StudioStore(database_path)
        room_id = store.create_room(
            "Manual ChatGPT isolated QA",
            "Only deterministic fake reviews are enabled.",
        )["room"]["id"]
        store.add_material(room_id, {
            "title": "Isolated evidence",
            "kind": "note",
            "content": "This fixture has no external or market data dependency.",
        })
        service = ManualChatGPTService(store, review_rate_card={})
        created = service.create(
            room_id,
            objective="验证 API 审查、决定卡与用户冻结交互。",
            mode="standard",
        )
        valid_import = _valid_import(created)
        valid_import_path = temp_root / "valid-manual-chatgpt-result.json"
        valid_import_path.write_text(
            json.dumps(valid_import, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        fixture_session = created
        if arguments.initial_state != "bundle-ready":
            fixture_session = service.dispatch(room_id, created["id"])
        if arguments.initial_state == "import-rejected":
            fixture_session = service.import_result(
                room_id,
                created["id"],
                json.dumps(_invalid_import(fixture_session), ensure_ascii=False),
            )
        elif arguments.initial_state == "api-review":
            fixture_session = service.import_result(
                room_id,
                created["id"],
                json.dumps(valid_import, ensure_ascii=False),
            )

        expected_state = {
            "bundle-ready": "BUNDLE_READY",
            "waiting": "WAITING_FOR_CHATGPT",
            "import-rejected": "IMPORT_REJECTED",
            "api-review": "API_REVIEW",
        }[arguments.initial_state]
        if fixture_session.get("state") != expected_state:
            raise RuntimeError(
                "isolated Manual ChatGPT fixture did not reach " + expected_state
            )

        fake_provider = FakeReviewProvider(
            ProviderResponse,
            MANUAL_CHATGPT_API_REVIEW_VERSION,
        )
        http_server.STORE = store
        http_server.PROVIDERS = ProviderRegistry({
            fake_provider.provider_id: fake_provider,
        })
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        if server.server_port == 8770 or server.server_port == 11111:
            server.server_close()
            raise RuntimeError("ephemeral QA server selected a protected port")
        payload = {
            "url": f"http://127.0.0.1:{server.server_port}/",
            "room_id": room_id,
            "session_id": created["id"],
            "initial_state": expected_state,
            "valid_import_fixture_path": str(valid_import_path),
            "valid_import_fixture_uses_formal_data": False,
            "provider": fake_provider.provider_id,
            "model": "fake-review-v1",
            "formal_assets_used": False,
            "real_provider_calls_allowed": False,
            "market_connections_allowed": False,
            "maximum_lifetime_seconds": arguments.lifetime_seconds,
            "auto_shutdown_when_frozen": not arguments.keep_open_after_frozen,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
        stop_watchdog = threading.Event()

        def shutdown_watchdog() -> None:
            deadline = time.monotonic() + arguments.lifetime_seconds
            while not stop_watchdog.wait(0.2):
                should_stop = time.monotonic() >= deadline
                if not should_stop and not arguments.keep_open_after_frozen:
                    try:
                        current = service.latest(room_id)
                    except Exception:
                        current = None
                    should_stop = bool(current and current.get("state") == "FROZEN")
                if should_stop:
                    server.shutdown()
                    return

        watchdog = threading.Thread(
            target=shutdown_watchdog,
            name="manual-chatgpt-qa-shutdown",
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
