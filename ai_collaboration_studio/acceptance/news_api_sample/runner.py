"""Prepare/inspect by default. Real execution needs separately approved hashes.

No production DB, environment credentials, feed fetching or provider probes.
The one-attempt control belongs to this executor, not the product session API.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import getpass
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from types import ModuleType
from urllib.parse import urlsplit
import warnings

PROJECT = Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from backend.news_content_quality import QUALITY_VERSION, TEXT_FORMAT, build_input, inspect_material
from backend.news_platform_ai import INSTRUCTIONS, MAX_OUTPUT, MODEL, NewsAnalysis
from backend.news_platform_routes import news_response
from backend.path_identity import first_reparse_component

ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/responses"
LIMIT = 124536
TTL = 900
FIELDS = ("id", "source_id", "source_name", "url", "title", "excerpt", "published_at", "discovered_at", "updated_at")
_PROCESS_LOCK = threading.Lock()
_PROCESS_TRANSPORT_SPENT = False


class Refused(ValueError):
    """Controlled message only: never attach credentials/provider bodies."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise Refused("timezone_required")
    return parsed.timestamp()


def safe_path(value):
    path = Path(value)
    if not path.is_absolute() or first_reparse_component(path) is not None:
        raise Refused("absolute_non_alias_path_required")
    if path.exists() and path.is_file() and path.stat().st_nlink != 1:
        raise Refused("hardlink_not_allowed")
    return path.resolve()


def read_json(path, expected=None):
    data = safe_path(path).read_bytes()
    if len(data) > 2_000_000 or (expected is not None and digest(data) != expected):
        raise Refused("file_hash_or_size_mismatch")
    return json.loads(data.decode("utf-8-sig"))


def exclusive_json(path, value):
    """Creation is the irreversible claim; incomplete receipts remain spent."""
    with safe_path(path).open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def source_hashes():
    paths = sorted((PROJECT / "backend").rglob("*.py"))
    paths += sorted(Path(__file__).parent.glob("*.py"))
    return {str(p.relative_to(PROJECT)).replace("\\", "/"): digest(p.read_bytes()) for p in paths}


def sample(path, expected):
    raw = read_json(path, expected)
    item = {field: raw.get(field) for field in FIELDS}
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", item.get("id") or ""):
        raise Refused("invalid_sample_id")
    if any(value is not None and (type(value) is not str or len(value) > 16000) for value in item.values()):
        raise Refused("invalid_sample_fields")
    parts = urlsplit(item.get("url") or "")
    if parts.scheme != "https" or parts.username or parts.password or parts.hostname not in {
        "www.federalreserve.gov", "www.bls.gov", "investors.seagate.com",
    }:
        raise Refused("public_official_sample_required")
    if not inspect_material(item)["can_analyze"]:
        raise Refused("substantive_saved_excerpt_required")
    return item


def fingerprint(item):
    # This method only hashes the public item; no constructor/DB is used.
    return NewsAnalysis.__new__(NewsAnalysis)._fingerprint(item)


def body_for(text):
    return {"model": MODEL, "instructions": INSTRUCTIONS, "input": text,
            "max_output_tokens": MAX_OUTPUT, "thinking": {"type": "disabled"},
            "store": False, "text": {"format": copy.deepcopy(TEXT_FORMAT)}}


def reservation(body):
    size = len(json.dumps(body).encode())
    amount = (size + 256) * 6 + MAX_OUTPUT * 30
    if size > 16000 or amount > LIMIT or MAX_OUTPUT != 900 or MODEL != "doubao-seed-2-1-pro-260915":
        raise Refused("request_allowance_exceeded")
    return size, amount


def prepare(sample_path, sample_sha, output):
    item = sample(sample_path, sample_sha)
    material = build_input(item, checked_at=time.time())
    size, amount = reservation(body_for(json.dumps(material, ensure_ascii=False, sort_keys=True)))
    directory = safe_path(output)
    directory.mkdir(parents=True, exist_ok=False)
    plan = {
        "schema": "news_single_rss_plan_v1", "grant_id": secrets.token_hex(16),
        "created_at": utc(), "run_directory": str(directory),
        "sample_path": str(safe_path(sample_path)), "sample_sha256": sample_sha,
        "item_id": item["id"], "snapshot_sha256": digest(canonical(item)), "fingerprint": fingerprint(item),
        "source_hashes": source_hashes(), "model": MODEL, "quality_version": QUALITY_VERSION,
        "instructions_sha256": digest(INSTRUCTIONS.encode()), "schema_sha256": digest(canonical(TEXT_FORMAT)),
        "endpoint": ENDPOINT, "max_calls": 1, "max_output_tokens": 900, "timeout_seconds": 45,
        "activation_ttl_seconds": TTL, "max_request_bytes": 16000, "max_reserved_micro_cny": LIMIT,
        "preview_request_bytes": size, "preview_reserved_micro_cny": amount,
        "supplier_bill_hard_limit": False, "automatic": False, "live_authorized": False,
        "source_publication": item["published_at"], "preview_freshness": material["freshness"],
        "scope": "Saved RSS title/excerpt only; independent result DB; no production writeback or collector.",
        "clock_rule": "Execution checked_at and derived freshness use actual time; all other input is frozen. Guard recomputes full material at that exact checked_at.",
        "semantic_review": ["Correct subject and event", "No invented numbers, conditions, dates or full-text claims",
                            "Quote entails each claim", "Old publication is not a new event", "Only RSS read; uncertainty stated"],
    }
    exclusive_json(directory / "plan.json", plan)
    plan_sha = digest((directory / "plan.json").read_bytes())
    exclusive_json(directory / "authorization.template.json", {
        "schema": "news_single_rss_authorization_v1", "approved": False,
        "plan_sha256": plan_sha, "grant_id": plan["grant_id"], "max_calls": 1,
        "max_reserved_micro_cny": LIMIT, "approved_at": None, "expires_at": None,
        "approval_reference": "Requires separate explicit human authorization; do not infer from this template.",
    })
    return {"status": "prepared_not_authorized", "plan": str(directory / "plan.json"),
            "plan_sha256": plan_sha, "model": MODEL, "max_calls": 1,
            "preview_reserved_micro_cny": amount, "maximum_reserved_micro_cny": LIMIT,
            "supplier_bill_hard_limit": False, "real_requests": 0}


def inspect_plan(plan_path, expected):
    path = safe_path(plan_path)
    plan = read_json(path, expected)
    if plan.get("schema") != "news_single_rss_plan_v1" or safe_path(plan["run_directory"]) != path.parent:
        raise Refused("plan_binding_mismatch")
    item = sample(plan["sample_path"], plan["sample_sha256"])
    fixed = {"model": MODEL, "quality_version": QUALITY_VERSION, "endpoint": ENDPOINT,
             "max_calls": 1, "max_output_tokens": 900, "timeout_seconds": 45,
             "activation_ttl_seconds": TTL, "max_request_bytes": 16000, "max_reserved_micro_cny": LIMIT,
             "automatic": False, "supplier_bill_hard_limit": False, "live_authorized": False}
    if any(type(plan.get(k)) is not type(v) or plan.get(k) != v for k, v in fixed.items()):
        raise Refused("plan_limits_mismatch")
    if (plan["source_hashes"] != source_hashes() or plan["item_id"] != item["id"]
            or plan["snapshot_sha256"] != digest(canonical(item)) or plan["fingerprint"] != fingerprint(item)
            or plan["instructions_sha256"] != digest(INSTRUCTIONS.encode())
            or plan["schema_sha256"] != digest(canonical(TEXT_FORMAT))):
        raise Refused("source_sample_contract_changed")
    return plan, item


def authorize(plan, plan_sha, auth_path, auth_sha):
    auth = read_json(auth_path, auth_sha)
    expected = {"schema": "news_single_rss_authorization_v1", "approved": True, "plan_sha256": plan_sha,
                "grant_id": plan["grant_id"], "max_calls": 1, "max_reserved_micro_cny": LIMIT}
    if any(type(auth.get(k)) is not type(v) or auth.get(k) != v for k, v in expected.items()):
        raise Refused("separate_explicit_authorization_required")
    start, end = timestamp(auth["approved_at"]), timestamp(auth["expires_at"])
    if not start <= time.time() < end or not 0 < end - start <= TTL:
        raise Refused("authorization_expired_or_invalid")
    if not isinstance(auth.get("approval_reference"), str) or not auth["approval_reference"].strip():
        raise Refused("human_approval_reference_required")
    return auth


class SnapshotNews:
    def __init__(self, item):
        self._item = copy.deepcopy(item)
        self.ai = None

    def get_item(self, item_id):
        return copy.deepcopy(self._item) if item_id == self._item["id"] else None

    def list_items(self, **_):
        return {"items": [copy.deepcopy(self._item)], "total": 1}

    def overview(self):
        return {"enabled": False, "collector_running": False, "isolated_acceptance": True}


class OneShot:
    def __init__(self, plan, item, auth):
        self.plan, self.item, self.auth = plan, copy.deepcopy(item), auth
        self.directory = safe_path(plan["run_directory"])
        if (self.directory / "spent.json").exists():
            raise Refused("grant_already_spent_or_unknown")
        self.deadline = time.monotonic() + max(0, min(TTL, timestamp(auth["expires_at"]) - time.time()))
        self.lock = threading.Lock()
        self.ai = None
        self.consumed = False
        self.transport_calls = 0
        self.expected_wire = None
        self.original_open = None
        self.provider_receipt = {}

    def valid_time(self):
        if time.monotonic() >= self.deadline or not timestamp(self.auth["approved_at"]) <= time.time() < timestamp(self.auth["expires_at"]):
            raise Refused("grant_expired")

    def generate(self, key, text):
        with self.lock:
            self.valid_time()
            if self.consumed or (self.directory / "spent.json").exists():
                raise Refused("grant_spent")
            material = json.loads(text)
            checked = timestamp(material["checked_at"])
            if not timestamp(self.auth["approved_at"]) <= checked <= time.time() + 1 or abs(time.time() - checked) > 10:
                raise Refused("input_clock_not_current")
            if canonical(material) != canonical(build_input(self.item, checked_at=checked)):
                raise Refused("input_changed")
            size, amount = reservation(body_for(text))
            with self.ai._db() as db:
                rows = db.execute("SELECT item_id,fingerprint,status,reserved FROM runs").fetchall()
            if len(rows) != 1 or tuple(rows[0]) != (self.item["id"], self.plan["fingerprint"], "pending", amount):
                raise Refused("normal_pending_reservation_required")
            self.expected_wire = json.dumps(body_for(text), ensure_ascii=False).encode()
            exclusive_json(self.directory / "spent.json", {
                "state": "consumed_outcome_unknown_until_terminal", "grant_id": self.plan["grant_id"],
                "item_id": self.item["id"], "fingerprint": self.plan["fingerprint"],
                "request_sha256": digest(self.expected_wire), "reservation_request_bytes": size,
                "wire_request_bytes": len(self.expected_wire), "reserved_micro_cny": amount, "at": utc(),
            })
            self.consumed = True
        response = NewsAnalysis._doubao(key, text)
        self.provider_receipt = {"ok": bool(response.ok), "model": response.model,
                                 "error_code": response.error_code,
                                 "remote_outcome": "response_received" if response.ok or response.error_code in {
                                     "http_status", "invalid_response", "empty_response"} else "UNKNOWN",
                                 "usage": {
                                     k: v for k, v in (response.usage or {}).items()
                                     if k in {"input_tokens", "output_tokens", "total_tokens"} and type(v) is int and v >= 0}}
        return response

    def open(self, request, *, timeout):
        global _PROCESS_TRANSPORT_SPENT
        with _PROCESS_LOCK:
            self.valid_time()
            if (_PROCESS_TRANSPORT_SPENT or self.transport_calls or not self.consumed
                    or request.full_url != ENDPOINT or request.get_method() != "POST" or timeout != 45
                    or request.data != self.expected_wire or self.original_open is None):
                raise Refused("outbound_request_not_authorized")
            exclusive_json(self.directory / "transport-started.json", {
                "at": utc(), "request_sha256": digest(request.data), "model_post_attempts": 1,
                "outcome": "UNKNOWN", "retry_allowed": False,
            })
            _PROCESS_TRANSPORT_SPENT = True
            self.transport_calls = 1
        # Exactly one application-level HTTPS open. No retries, even on timeout.
        return self.original_open(request, timeout=timeout)


def provider_module():
    """Import real adapter without consulting existing configuration or proxies."""
    if "backend.config" in sys.modules:
        raise Refused("fresh_executor_process_required")
    config = ModuleType("backend.config")
    config.ARK_API_KEY = config.OPENAI_API_KEY = ""
    config.ARK_BASE_URL, config.ARK_MODEL = ENDPOINT.rsplit("/", 1)[0], MODEL
    config.OPENAI_BASE_URL, config.OPENAI_MODEL = "https://api.openai.com/v1", "disabled"
    sys.modules["backend.config"] = config
    import urllib.request
    original = urllib.request.getproxies
    urllib.request.getproxies = lambda: {}
    try:
        return importlib.import_module("backend.providers.doubao_provider")
    finally:
        urllib.request.getproxies = original


def server_for(runtime):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        configured = False

        def log_message(self, *_):
            pass

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

        def handle_request(self, method):
            status, result = 403, {"ok": False, "error": "acceptance_request_refused"}
            allowed_get = {"/api/news/items", "/api/news/items/" + runtime._item["id"], "/api/news/ai", "/api/news/overview"}
            allowed_post = {"/api/news/ai/session", "/api/news/ai/clear", "/api/news/items/" + runtime._item["id"] + "/analyze"}
            if (hmac.compare_digest(self.headers.get("X-Acceptance-Nonce", ""), token)
                    and self.headers.get("Host") == "127.0.0.1:" + str(self.server.server_port)
                    and not self.headers.get("Origin") and self.path in (allowed_get if method == "GET" else allowed_post)):
                payload = None
                try:
                    if method == "POST":
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 8192 or self.headers.get("Transfer-Encoding"):
                            raise Refused("invalid_body")
                        payload = json.loads(self.rfile.read(length))
                        if self.path == "/api/news/ai/session":
                            if type(payload) is not dict or payload.get("automatic") is not False or Handler.configured:
                                raise Refused("one_manual_configuration_only")
                            Handler.configured = True
                    result, status = news_response(runtime, self.path, payload=payload, method=method)
                except (ValueError, TypeError):
                    status, result = 400, {"ok": False, "error": "acceptance_request_refused"}
            encoded = canonical(result)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if server.server_port == 8770:
        server.server_close()
        raise Refused("production_port_forbidden")
    server.daemon_threads = True
    return server, token


def request_local(server, token, method, path, payload=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(method, path, body=canonical(payload) if payload is not None else None,
                           headers={"X-Acceptance-Nonce": token, "Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def run_http(plan, item, auth, key, adapter, *, offline_opener=None):
    """Tests replace only the final opener; CLI never accepts that override."""
    guard = OneShot(plan, item, auth)
    runtime = SnapshotNews(item)
    runtime.ai = NewsAnalysis(Path(plan["run_directory"]) / "analysis.sqlite3", runtime, generate=guard.generate)
    guard.ai = runtime.ai
    original = adapter.open_provider_request
    guard.original_open = offline_opener or original
    adapter.open_provider_request = guard.open
    server, token = server_for(runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    terminal = {"status": "UNKNOWN", "grant_id": plan["grant_id"], "real_provider": offline_opener is None,
                "supplier_bill_verified": False, "production_changed": False, "semantic_review_required": True}
    try:
        runtime.ai.start()
        thread.start()
        status, configured = request_local(server, token, "POST", "/api/news/ai/session", {
            "api_key": key, "automatic": False, "acknowledge_budget": True})
        key = ""
        if status != 200 or configured.get("ai", {}).get("automatic") is not False:
            raise Refused("isolated_session_failed")
        path = "/api/news/items/" + item["id"]
        status, _ = request_local(server, token, "POST", path + "/analyze", {})
        if status != 202:
            raise Refused("request_not_admitted")
        wait_until = time.monotonic() + 55
        while time.monotonic() < wait_until:
            status, detail = request_local(server, token, "GET", path)
            result = detail.get("item", {}).get("ai_analysis")
            if result and result.get("status") in {"completed", "failed", "unknown", "material_insufficient"}:
                terminal["status"] = result["status"]
                terminal["result"] = result
                break
            time.sleep(0.05)
        terminal.update(model_post_attempts=guard.transport_calls, provider=guard.provider_receipt,
                        ai=runtime.ai.status(), source_snapshot_sha256=digest(canonical(runtime._item)),
                        loopback_port=server.server_port, ended_at=utc())
        exclusive_json(Path(plan["run_directory"]) / "terminal.json", terminal)
        return terminal
    finally:
        key = ""
        runtime.ai.clear()
        runtime.ai.stop(timeout=50)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        # Never restore the real adapter's unguarded opener in this process.
        # The CLI exits now; direct second calls remain denied by the spent guard.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    prep = commands.add_parser("prepare", help="Freeze public sample and make an unapproved plan; no network/key")
    prep.add_argument("--sample", required=True)
    prep.add_argument("--sample-sha256", required=True)
    prep.add_argument("--out", required=True)
    check = commands.add_parser("inspect", help="Verify local hashes only; no network/key")
    live = commands.add_parser("execute", help="One separately authorized real attempt, interactive key only")
    for target in (check, live):
        target.add_argument("--plan", required=True)
        target.add_argument("--plan-sha256", required=True)
    live.add_argument("--authorization", required=True)
    live.add_argument("--authorization-sha256", required=True)
    live.add_argument("--allow-real-provider", action="store_true")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "prepare":
            result = prepare(args.sample, args.sample_sha256, args.out)
        else:
            plan, item = inspect_plan(args.plan, args.plan_sha256)
            if args.command == "inspect":
                result = {"status": "local_bindings_valid", "real_requests": 0,
                          "grant_id": plan["grant_id"], "spent_or_started": any((Path(plan["run_directory"]) / name).exists()
                          for name in ("started.json", "spent.json", "transport-started.json"))}
            else:
                if not args.allow_real_provider:
                    raise Refused("real_execution_disabled_by_default")
                auth = authorize(plan, args.plan_sha256, args.authorization, args.authorization_sha256)
                directory = Path(plan["run_directory"])
                if any((directory / n).exists() for n in ("started.json", "spent.json", "analysis.sqlite3", "terminal.json")):
                    raise Refused("run_already_started_no_retry")
                exclusive_json(directory / "started.json", {"at": utc(), "plan_sha256": args.plan_sha256,
                               "authorization_sha256": args.authorization_sha256, "outcome": "UNKNOWN", "retry_allowed": False})
                adapter = provider_module()
                if not sys.stdin.isatty():
                    raise Refused("interactive_masked_key_input_required")
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    key = getpass.getpass("本次独立验收 API key（不保存，最多一次请求）: ")
                try:
                    result = run_http(plan, item, auth, key, adapter)
                finally:
                    key = ""
                result = {k: result[k] for k in ("status", "model_post_attempts", "supplier_bill_verified", "production_changed")}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        # Do not print arbitrary exception text/traceback, which may carry input.
        print(json.dumps({"status": "REFUSED_OR_UNKNOWN", "retry_allowed": False,
                          "message": "检查授权/哈希/期限及本地安全回执；不自动重试。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
