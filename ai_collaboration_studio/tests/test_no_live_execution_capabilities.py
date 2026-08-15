from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"

# This is a defense-in-depth static audit, not the sole execution boundary.
# Runtime API-path and provider-payload guards live in backend.execution_boundary.
# Prose such as "do not place orders" is intentionally ignored.
FORBIDDEN_EXECUTION_SYMBOLS = {
    "account_context",
    "basetradecontext",
    "brokerage_account",
    "broadcast_transaction",
    "cancel_all_order",
    "cancel_order",
    "capture_payment",
    "confirm_payment",
    "create_checkout_session",
    "create_order",
    "create_payment",
    "create_payment_intent",
    "create_wallet",
    "execute_order",
    "execute_trade",
    "get_account",
    "get_accounts",
    "issue_refund",
    "modify_order",
    "opencntradecontext",
    "openfuturetradecontext",
    "openhktradecontext",
    "opensecuritytradecontext",
    "opensectradecontext",
    "opentradecontext",
    "openustradecontext",
    "open_wallet",
    "payment_intent",
    "place_bet",
    "place_order",
    "place_trade",
    "payout_funds",
    "refund_payment",
    "send_order",
    "send_payment",
    "send_raw_transaction",
    "send_transaction",
    "sign_transaction",
    "submit_order",
    "submit_transaction",
    "tradecontext",
    "transfer_funds",
    "unlock_trade",
    "wallet_client",
    "withdraw_funds",
}
FORBIDDEN_EXECUTION_CANONICAL = {
    re.sub(r"[^a-z0-9]", "", symbol.lower())
    for symbol in FORBIDDEN_EXECUTION_SYMBOLS
}
MUTATING_HTTP_METHODS = {"post", "put", "patch", "delete"}

# These request keys opt an LLM provider into callable tools/functions.  Reading
# model text that happens to mention a tool is not prohibited by this gate.
FORBIDDEN_PROVIDER_REQUEST_KEYS = {
    "function_call",
    "functions",
    "parallel_tool_calls",
    "tool_choice",
    "tools",
}
FORBIDDEN_PROVIDER_TOOL_SYMBOLS = {
    "bind_tools",
    "function_tool",
    "register_tool",
}

# Route segments are matched exactly, so research copy containing words such as
# "trade" or "order" cannot trip the test.  Paper portfolios remain explicitly
# non-executing simulation surfaces.
FORBIDDEN_ROUTE_SEGMENTS = {
    "account",
    "accounts",
    "bet",
    "bets",
    "brokerage",
    "execute",
    "execution",
    "order",
    "orders",
    "payment",
    "payments",
    "trade",
    "trades",
    "transfer",
    "transfers",
    "wallet",
    "wallets",
    "withdraw",
    "withdrawals",
}
SAFE_SIMULATION_ROUTE_SEGMENTS = {
    "paper-portfolios",
    "walk-forward",
}
EXECUTING_ROUTE_VERBS = {
    "broadcast",
    "cancel",
    "capture",
    "confirm",
    "create",
    "execute",
    "modify",
    "open",
    "place",
    "refund",
    "send",
    "sign",
    "submit",
    "transfer",
    "unlock",
    "withdraw",
}
SAFE_RESEARCH_ROUTE_WORDS = {
    "analysis",
    "analytics",
    "backtest",
    "backtests",
    "evidence",
    "market",
    "observation",
    "observations",
    "paper",
    "portfolio",
    "portfolios",
    "research",
    "simulated",
    "simulation",
    "simulations",
}
ORDER_TRADE_ROUTE_WORDS = {"order", "orders", "trade", "trades"}


def _python_trees(root: Path):
    for path in sorted(root.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _constant_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left)
        right = _constant_string(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            parts.append(value.value)
        return "".join(parts)
    return None


def _canonical_symbol(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _symbol_references(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.lineno, node.name
        elif isinstance(node, ast.Name):
            yield node.lineno, node.id
        elif isinstance(node, ast.Attribute):
            yield node.lineno, node.attr
        elif isinstance(node, ast.alias):
            yield getattr(node, "lineno", 0), node.name.rsplit(".", 1)[-1]
            if node.asname:
                yield getattr(node, "lineno", 0), node.asname
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
        ):
            # Catch constant folding such as getattr(futu, "place_" + "order")
            # without scanning unrelated prose string literals.
            dynamic_name = _constant_string(node.args[1])
            if dynamic_name is not None:
                yield node.lineno, dynamic_name


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _mutating_http_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node.func)
        raw_leaf = call_name.rsplit(".", 1)[-1]
        leaf = raw_leaf.lower()
        if leaf in MUTATING_HTTP_METHODS and (
            call_name.startswith("requests.")
            or call_name.startswith("httpx.")
            or call_name.count(".") >= 1
        ):
            yield node.lineno, call_name
            continue
        if leaf == "request":
            method = _constant_string(_keyword(node, "method"))
            if method is None and raw_leaf != "Request" and node.args:
                method = _constant_string(node.args[0])
            data_argument = _keyword(node, "data")
            has_data = (
                (data_argument is not None and not (
                    isinstance(data_argument, ast.Constant) and data_argument.value is None
                ))
                or (raw_leaf == "Request" and len(node.args) >= 2)
            )
            if has_data or (method and method.lower() in MUTATING_HTTP_METHODS):
                yield node.lineno, call_name
            continue
        if leaf == "urlopen" and (
            _keyword(node, "data") is not None or len(node.args) >= 2
        ):
            yield node.lineno, call_name


def _route_segments(route: str) -> set[str]:
    return {
        segment.lower()
        for segment in route.split("/")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", segment)
    }


def _identifier_words(value: str) -> set[str]:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return set(re.findall(r"[a-z0-9]+", camel_split.lower()))


def _execution_route_hits(route: str) -> set[str]:
    """Classify executable API surfaces without scanning prose or comments.

    Account, wallet and payment resources are always forbidden.  Order/trade
    vocabulary is ignored only when the route is explicitly research or
    simulation scoped and has no execution verb; this keeps routes such as a
    paper-portfolio analysis from becoming false positives while still catching
    placeOrder, submit-trade and bare /orders resources.
    """

    segments = [segment for segment in str(route or "").split("/") if segment]
    words = set().union(*(_identifier_words(segment) for segment in segments)) if segments else set()
    canonical_segments = {_canonical_symbol(segment) for segment in segments}
    hits = words & (FORBIDDEN_ROUTE_SEGMENTS - ORDER_TRADE_ROUTE_WORDS)
    order_trade_hits = words & ORDER_TRADE_ROUTE_WORDS
    if order_trade_hits and (
        words & EXECUTING_ROUTE_VERBS
        or not words & SAFE_RESEARCH_ROUTE_WORDS
    ):
        hits.update(order_trade_hits)
    hits.update(canonical_segments & FORBIDDEN_EXECUTION_CANONICAL)
    return hits


def _contains_path_reference(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Attribute) and child.attr == "path"
        for child in ast.walk(node)
    )


def _api_route_literals(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and child.value.startswith("/api/")
    }


def _declared_api_routes(tree: ast.AST) -> set[str]:
    """Extract only strings used to dispatch request paths.

    Arbitrary strings that merely document an API or discuss orders are not
    route declarations and therefore cannot create static-audit false positives.
    """

    routes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and _contains_path_reference(node):
            routes.update(_api_route_literals(node))
            continue
        if not isinstance(node, ast.Call):
            continue
        leaf = _call_name(node.func).rsplit(".", 1)[-1]
        if leaf in {"fullmatch", "match", "search"} and _contains_path_reference(node):
            routes.update(_api_route_literals(node))
        elif (
            leaf in {"startswith", "endswith"}
            and isinstance(node.func, ast.Attribute)
            and _contains_path_reference(node.func.value)
        ):
            routes.update(_api_route_literals(node))
    return routes


class LiveExecutionStaticAuditTests(unittest.TestCase):
    def test_symbol_gate_ignores_prose_but_detects_callable_api(self) -> None:
        prose = ast.parse('note = "research only: place_order / 交易 / 订单"')
        callable_api = ast.parse(
            'broker.placeOrder(symbol="US.MU")\n'
            'getattr(client, "place_" + "order")("US.MU")'
        )

        prose_hits = {
            _canonical_symbol(symbol)
            for _lineno, symbol in _symbol_references(prose)
        } & FORBIDDEN_EXECUTION_CANONICAL
        callable_hits = {
            _canonical_symbol(symbol)
            for _lineno, symbol in _symbol_references(callable_api)
        } & FORBIDDEN_EXECUTION_CANONICAL

        self.assertEqual(prose_hits, set())
        self.assertEqual(callable_hits, {"placeorder"})

    def test_full_backend_and_declared_http_surface_are_read_only(self) -> None:
        safe_source = ast.parse(
            '# place_order, wallet and payment are forbidden in production\n'
            'note = "研究术语：订单、交易、模拟交易、place_order"\n'
            'def analyze_paper_portfolio():\n'
            '    return note\n'
        )
        safe_symbol_hits = {
            _canonical_symbol(symbol)
            for _lineno, symbol in _symbol_references(safe_source)
        } & FORBIDDEN_EXECUTION_CANONICAL
        self.assertEqual(safe_symbol_hits, set())
        self.assertEqual(_declared_api_routes(safe_source), set())

        safe_routes = {
            "/api/research/trade-thesis",
            "/api/simulations/portfolio_1/order-assumptions",
            "/api/paper-portfolios/portfolio_1/walk-forward",
        }
        self.assertTrue(all(not _execution_route_hits(route) for route in safe_routes))
        dangerous_routes = {
            "/api/orders",
            "/api/placeOrder",
            "/api/trade-context",
            "/api/wallets/main",
            "/api/payment-intents",
        }
        self.assertTrue(all(_execution_route_hits(route) for route in dangerous_routes))

        symbol_violations: list[str] = []
        route_violations: list[str] = []
        declared_routes: set[str] = set()
        for path, tree in _python_trees(BACKEND_ROOT):
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            for lineno, symbol in _symbol_references(tree):
                if _canonical_symbol(symbol) in FORBIDDEN_EXECUTION_CANONICAL:
                    symbol_violations.append(f"{relative}:{lineno} ({symbol})")
            for route in _declared_api_routes(tree):
                declared_routes.add(route)
                hits = sorted(_execution_route_hits(route))
                if hits:
                    route_violations.append(
                        f"{relative}: {route} ({', '.join(hits)})"
                    )

        self.assertTrue(declared_routes, "No dispatched /api route declarations were found")
        self.assertEqual(
            symbol_violations,
            [],
            "Live order/trade-context/wallet/payment callable surface found:\n"
            + "\n".join(symbol_violations),
        )
        self.assertEqual(
            route_violations,
            [],
            "Live execution HTTP route declaration found:\n"
            + "\n".join(route_violations),
        )

    def test_entire_backend_has_no_execution_symbols(self) -> None:
        violations: list[str] = []
        for path, tree in _python_trees(BACKEND_ROOT):
            for lineno, symbol in _symbol_references(tree):
                canonical = _canonical_symbol(symbol)
                if canonical in FORBIDDEN_EXECUTION_CANONICAL:
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{lineno} ({symbol})"
                    )

        self.assertEqual(
            violations,
            [],
            "Live trade/payment/wallet execution symbol(s) found:\n"
            + "\n".join(violations),
        )

    def test_mutating_http_calls_are_centralized_and_raw_broker_posts_are_detected(self) -> None:
        raw_broker = ast.parse(
            'requests.post("https://broker.example/v1/orders", json={})\n'
            'httpx.request("POST", "https://broker.example/v1/placeOrder")\n'
            'Request("https://broker.example/v1/orders", b"symbol=US.MU")'
        )
        self.assertEqual(len(list(_mutating_http_calls(raw_broker))), 3)

        violations: list[str] = []
        for path, tree in _python_trees(BACKEND_ROOT):
            builder_lines: set[int] = set()
            if path.name == "execution_boundary.py":
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "build_text_provider_request":
                        builder_lines.update(range(node.lineno, int(node.end_lineno or node.lineno) + 1))
            for lineno, call_name in _mutating_http_calls(tree):
                if (
                    path.name == "execution_boundary.py"
                    and lineno in builder_lines
                    and call_name == "urllib.request.Request"
                ):
                    continue
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{lineno} ({call_name})"
                )
        self.assertEqual(
            violations,
            [],
            "Mutating outbound HTTP must use backend.execution_boundary:\n"
            + "\n".join(violations),
        )

    def test_futu_sdk_is_confined_to_quote_context_adapter(self) -> None:
        allowed_path = "backend/market/futu_readonly.py"
        violations: list[str] = []
        context_symbols: set[str] = set()
        for path, tree in _python_trees(BACKEND_ROOT):
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif node.module:
                        modules = [node.module]
                    if any(module == "futu" or module.startswith("futu.") for module in modules):
                        if relative != allowed_path:
                            violations.append(f"{relative}:{node.lineno} (futu import)")
                if relative == allowed_path and isinstance(node, ast.Attribute):
                    if re.fullmatch(r"Open[A-Za-z0-9_]*Context", node.attr):
                        context_symbols.add(node.attr)
        self.assertEqual(violations, [])
        self.assertEqual(context_symbols, {"OpenQuoteContext"})

    def test_http_api_exposes_no_execution_route(self) -> None:
        path = BACKEND_ROOT / "http_server.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        routes = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("/api/")
        }
        self.assertTrue(routes, "No HTTP API route literals were discovered")

        violations = []
        for route in sorted(routes):
            segments = _route_segments(route)
            dangerous = sorted(segments & FORBIDDEN_ROUTE_SEGMENTS)
            if dangerous:
                violations.append(f"{route} ({', '.join(dangerous)})")

        self.assertEqual(
            violations,
            [],
            "Live execution HTTP route(s) found:\n" + "\n".join(violations),
        )
        discovered_segments = set().union(*(_route_segments(route) for route in routes))
        self.assertTrue(
            SAFE_SIMULATION_ROUTE_SEGMENTS <= discovered_segments,
            "Expected read-only/simulation route allowlist is incomplete",
        )

    def test_provider_requests_cannot_enable_tool_calling(self) -> None:
        scan_paths = [
            *sorted((BACKEND_ROOT / "providers").rglob("*.py")),
            BACKEND_ROOT / "orchestrator.py",
            BACKEND_ROOT / "provider_preflight.py",
        ]
        violations: list[str] = []
        for path in scan_paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key in node.keys:
                        if (
                            isinstance(key, ast.Constant)
                            and isinstance(key.value, str)
                            and key.value.lower() in FORBIDDEN_PROVIDER_REQUEST_KEYS
                        ):
                            violations.append(
                                f"{path.relative_to(PROJECT_ROOT)}:{key.lineno} ({key.value})"
                            )
                elif (
                    isinstance(node, ast.keyword)
                    and node.arg
                    and node.arg.lower() in FORBIDDEN_PROVIDER_REQUEST_KEYS
                ):
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} ({node.arg})"
                    )

            for lineno, symbol in _symbol_references(tree):
                if symbol.lower() in FORBIDDEN_PROVIDER_TOOL_SYMBOLS:
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{lineno} ({symbol})"
                    )

        self.assertEqual(
            sorted(set(violations)),
            [],
            "Provider tool/function calling capability found:\n"
            + "\n".join(sorted(set(violations))),
        )


if __name__ == "__main__":
    unittest.main()
