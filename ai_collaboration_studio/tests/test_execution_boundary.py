from __future__ import annotations

import json
import unittest

from backend.execution_boundary import (
    ExecutionBoundaryViolation,
    build_text_provider_request,
    ensure_safe_api_path,
    ensure_text_only_provider_payload,
)


class ExecutionBoundaryTests(unittest.TestCase):
    def test_api_paths_block_execution_words_including_camel_case(self) -> None:
        for path in (
            "/api/orders",
            "/api/placeOrder",
            "/api/accounts/member_1/transfer-funds",
            "https://broker.example/v1/wallets/main",
        ):
            with self.subTest(path=path), self.assertRaises(ExecutionBoundaryViolation):
                ensure_safe_api_path(path)

        ensure_safe_api_path("/api/paper-portfolios/portfolio_1/walk-forward")
        ensure_safe_api_path("/api/rooms/room_storage/observations")

    def test_provider_payload_is_text_only_and_ignores_prose(self) -> None:
        ensure_text_only_provider_payload({
            "model": "model-test",
            "input": "Research text may mention place_order, accounts, 交易 and 订单.",
        })
        for payload in (
            {"tools": []},
            {"toolChoice": "auto"},
            {"nested": {"function_call": "auto"}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ExecutionBoundaryViolation):
                ensure_text_only_provider_payload(payload)

    def test_text_provider_request_is_the_only_allowed_mutating_http_shape(self) -> None:
        request = build_text_provider_request(
            "https://provider.example/v1",
            "responses",
            {"model": "model-test", "input": "Reply with OK."},
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            json.loads(bytes(request.data or b"").decode("utf-8"))["model"],
            "model-test",
        )

        with self.assertRaises(ExecutionBoundaryViolation):
            build_text_provider_request(
                "https://broker.example/v1/accounts/demo",
                "responses",
                {"model": "model-test", "input": "ignored"},
                headers={},
            )
        with self.assertRaises(ExecutionBoundaryViolation):
            build_text_provider_request(
                "https://provider.example/v1",
                "responses",
                {"model": "model-test", "tools": []},
                headers={},
            )

        for endpoint, payload in (
            ("submit", {"model": "model-test", "input": "ignored"}),
            ("responses", {"model": "model-test", "input": "ignored", "side": "BUY"}),
            ("responses", {"model": "model-test", "input": "ignored", "symbol": "US.MU"}),
            ("chat_completions", {"model": "model-test", "messages": [], "method": "placeOrder"}),
        ):
            with self.subTest(endpoint=endpoint, payload=payload), self.assertRaises(ExecutionBoundaryViolation):
                build_text_provider_request(
                    "https://provider.example/v1",
                    endpoint,
                    payload,
                    headers={},
                )


if __name__ == "__main__":
    unittest.main()
