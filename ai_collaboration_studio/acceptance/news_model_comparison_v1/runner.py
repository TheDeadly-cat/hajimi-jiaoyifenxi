"""Isolated, immutable 5 x 2 comparison. Default modes never call providers."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
ROUTES = (("doubao", "doubao-seed-2-1-pro-260915"), ("deepseek", "deepseek-flash"))
FIELDS = ("id", "source_id", "source_name", "url", "title", "excerpt", "published_at", "discovered_at", "updated_at")
MAX_CALLS, MAX_BUDGET = 10, 1_000_000
SOURCE_FILES = ("execution_boundary.py", "news_analysis_providers.py", "news_content_quality.py")
_REAL_BUILD_OPENER = urllib.request.build_opener


class Refused(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise Refused("timezone_required")
    return parsed.timestamp()


def path(value):
    p = Path(value)
    if not p.is_absolute():
        raise Refused("absolute_path_required")
    for part in (p, *p.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise Refused("alias_path_forbidden")
    if p.is_file() and p.stat().st_nlink != 1:
        raise Refused("hardlink_forbidden")
    return p.resolve()


def read_json(p, expected=None):
    raw = path(p).read_bytes()
    if len(raw) > 2_000_000 or (expected and sha(raw) != expected):
        raise Refused("hash_or_size_mismatch")
    return json.loads(raw.decode("utf-8-sig"))


def write_new(p, value):
    with path(p).open("xb") as f:
        f.write(canonical(value) + b"\n")
        f.flush()
        os.fsync(f.fileno())


def module(p, name):
    spec = importlib.util.spec_from_file_location(name, p)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def modules(root):
    # Load only the hash-bound files, never the ambient backend package/config.
    # The empty package search path prevents an implicit dependency fallback.
    package_name = "comparison_frozen_" + sha(str(root).encode("utf-8"))[:16]
    package = types.ModuleType(package_name)
    package.__path__ = []
    package.__package__ = package_name
    sys.modules[package_name] = package
    module(root / "frozen-code/execution_boundary.py", package_name + ".execution_boundary")
    return (module(root / "frozen-code/news_analysis_providers.py", package_name + ".news_analysis_providers"),
            module(root / "frozen-code/news_content_quality.py", package_name + ".news_content_quality"))


def own_hashes():
    return {name: sha((HERE / name).read_bytes()) for name in ("runner.py", "credentials.py")}


def validate_samples(raw, quality, checked_at):
    if type(raw) is not list or len(raw) != 5:
        raise Refused("exactly_five_samples_required")
    items = []
    for item in raw:
        if type(item) is not dict or set(item) - set(FIELDS):
            raise Refused("sample_fields_invalid")
        if any(value is not None and (type(value) is not str or len(value) > 16000) for value in item.values()):
            raise Refused("sample_value_invalid")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item.get("id") or ""):
            raise Refused("sample_id_invalid")
        url = urlsplit(item.get("url") or "")
        if url.scheme != "https" or url.username or url.password or url.hostname not in {
            "www.federalreserve.gov", "www.bls.gov", "investors.seagate.com", "investor.nvidia.com",
            "investors.micron.com", "www.bea.gov", "home.treasury.gov", "www.sec.gov",
        }:
            raise Refused("official_public_source_required")
        if not quality.inspect_material(item, checked_at)["can_analyze"]:
            raise Refused("substantive_excerpt_required")
        items.append({key: item.get(key) for key in FIELDS})
    if len({item["id"] for item in items}) != 5 or len({(item["title"], item["excerpt"]) for item in items}) != 5:
        raise Refused("distinct_samples_required")
    return items


def requests_for(items, provider, quality, checked_at):
    requests = []
    for index, item in enumerate(items, 1):
        material = quality.build_input(item, checked_at=checked_at)
        text = json.dumps(material, ensure_ascii=False, sort_keys=True)
        for name, model_name in ROUTES:
            body = provider.build_body(name, model_name, text, quality.INSTRUCTIONS, quality.TEXT_FORMAT)
            reservation, basis = provider.reservation_for(name, model_name, body)
            if len(provider.encode_body(body)) > 16000:
                raise Refused("request_size_limit")
            requests.append({"slot": len(requests) + 1, "sample_index": index, "sample_id": item["id"],
                             "provider": name, "model": model_name, "body": body,
                             "body_sha256": sha(provider.encode_body(body)), "material": material,
                             "reserved_micro_cny": reservation, "reservation_basis": basis})
    if sum(r["reserved_micro_cny"] for r in requests) > MAX_BUDGET:
        raise Refused("batch_budget_exceeded")
    return requests


def prepare(samples_path, samples_sha, output, checked_at=None):
    root = path(output)
    root.mkdir(parents=True, exist_ok=False)
    (root / "frozen-code").mkdir()
    (root / "evidence").mkdir()
    frozen = {}
    for name in SOURCE_FILES:
        data = (PROJECT / "backend" / name).read_bytes()
        (root / "frozen-code" / name).write_bytes(data)
        frozen[name] = sha(data)
    provider, quality = modules(root)
    checked = timestamp(checked_at) if checked_at else time.time()
    items = validate_samples(read_json(samples_path, samples_sha), quality, checked)
    write_new(root / "samples.json", items)
    requests = requests_for(items, provider, quality, checked)
    plan = {"schema": "news_model_comparison_plan_v1", "run_directory": str(root),
            "grant_id": secrets.token_hex(16), "created_at": utc(), "checked_at": checked,
            "samples_sha256": sha((root / "samples.json").read_bytes()), "source_samples_sha256": samples_sha,
            "frozen_code_sha256": frozen, "executor_sha256": own_hashes(),
            "quality_version": quality.QUALITY_VERSION,
            "instructions_sha256": sha(quality.INSTRUCTIONS.encode()), "format_sha256": sha(canonical(quality.TEXT_FORMAT)),
            "routes": [list(route) for route in ROUTES], "max_calls": MAX_CALLS, "max_reserved_micro_cny": MAX_BUDGET,
            "max_output_tokens": 900, "socket_timeout_seconds": 45, "retries": 0,
            "credential_input": "masked_local_two_fields_process_memory_only", "production_writeback": False,
            "transport": "frozen adapter; instrumented final opener with explicit direct ProxyHandler({}); no environment proxy discovery",
            "supplier_bill_hard_limit": False, "automatic_fallback": False,
            "requests": [{key: r[key] for key in ("slot", "sample_index", "sample_id", "provider", "model", "body_sha256", "reserved_micro_cny", "reservation_basis")} for r in requests],
            "total_reserved_micro_cny": sum(r["reserved_micro_cny"] for r in requests)}
    write_new(root / "plan.json", plan)
    digest = sha((root / "plan.json").read_bytes())
    write_new(root / "authorization.template.json", {"schema": "news_model_comparison_authorization_v1", "approved": False,
              "grant_id": plan["grant_id"], "plan_sha256": digest, "max_calls": MAX_CALLS,
              "max_reserved_micro_cny": MAX_BUDGET, "approved_at": None, "expires_at": None, "approval_reference": None})
    return {"status": "prepared", "plan_sha256": digest, "total_reserved_micro_cny": plan["total_reserved_micro_cny"], "requests": len(requests)}


def inspect_plan(plan_path, expected):
    plan = read_json(plan_path, expected)
    root = path(plan_path).parent
    if (plan.get("schema") != "news_model_comparison_plan_v1" or path(plan["run_directory"]) != root
            or plan.get("executor_sha256") != own_hashes() or plan.get("routes") != [list(r) for r in ROUTES]
            or (plan.get("max_calls"), plan.get("max_reserved_micro_cny"), plan.get("max_output_tokens"), plan.get("socket_timeout_seconds"), plan.get("retries")) != (10, 1_000_000, 900, 45, 0)):
        raise Refused("plan_binding_mismatch")
    for name in SOURCE_FILES:
        if sha(path(root / "frozen-code" / name).read_bytes()) != plan["frozen_code_sha256"][name]:
            raise Refused("code_hash_mismatch")
    provider, quality = modules(root)
    items = validate_samples(read_json(root / "samples.json", plan["samples_sha256"]), quality, plan["checked_at"])
    requests = requests_for(items, provider, quality, plan["checked_at"])
    public = [{key: r[key] for key in ("slot", "sample_index", "sample_id", "provider", "model", "body_sha256", "reserved_micro_cny", "reservation_basis")} for r in requests]
    if (public != plan["requests"] or sum(r["reserved_micro_cny"] for r in requests) != plan["total_reserved_micro_cny"]
            or sha(quality.INSTRUCTIONS.encode()) != plan["instructions_sha256"] or sha(canonical(quality.TEXT_FORMAT)) != plan["format_sha256"]):
        raise Refused("request_binding_mismatch")
    return root, plan, requests, provider, quality


def check_authorization(auth, plan, plan_sha):
    now = time.time()
    if (auth.get("schema") != "news_model_comparison_authorization_v1" or auth.get("approved") is not True
            or auth.get("plan_sha256") != plan_sha or auth.get("grant_id") != plan["grant_id"]
            or auth.get("max_calls") != 10 or auth.get("max_reserved_micro_cny") != 1_000_000
            or type(auth.get("approval_reference")) is not str or not auth["approval_reference"].strip()
            or not timestamp(auth["approved_at"]) <= now < timestamp(auth["expires_at"])
            or not 0 < timestamp(auth["expires_at"]) - timestamp(auth["approved_at"]) <= 3600):
        raise Refused("authorization_invalid_or_expired")


class Ledger:
    def __init__(self, database):
        self.database = path(database)
        if self.database.exists():
            raise Refused("ledger_already_exists")
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE attempts(slot INTEGER PRIMARY KEY,sample_id TEXT,provider TEXT,model TEXT,reserved INTEGER,status TEXT,receipt TEXT,UNIQUE(sample_id,provider,model))")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.database), timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, request):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count, spent = db.execute("SELECT COUNT(*),COALESCE(SUM(reserved),0) FROM attempts").fetchone()
            if count >= MAX_CALLS or spent + request["reserved_micro_cny"] > MAX_BUDGET:
                raise Refused("ledger_limit")
            try:
                db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,'reserved_unknown','{}')", (request["slot"], request["sample_id"], request["provider"], request["model"], request["reserved_micro_cny"]))
            except sqlite3.IntegrityError:
                raise Refused("slot_already_spent") from None

    def finish(self, request, receipt):
        with self.connect() as db:
            db.execute("UPDATE attempts SET status=?,receipt=? WHERE slot=?", (receipt["status"], canonical(receipt).decode(), request["slot"]))


def contains_secret(value, secret_values):
    """Detect credentials after JSON/Unicode decoding, never log rejected text."""
    if isinstance(value, dict):
        return any(contains_secret(k, secret_values) or contains_secret(v, secret_values) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_secret(v, secret_values) for v in value)
    if not isinstance(value, str):
        return False
    text = value
    for _ in range(4):
        if any(secret and secret in text for secret in secret_values):
            return True
        try:
            decoded = json.loads(text)
        except Exception:
            decoded = None
        if isinstance(decoded, (dict, list)) and contains_secret(decoded, secret_values):
            return True
        next_text = decoded if isinstance(decoded, str) else re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)), text)
        if next_text == text:
            break
        text = next_text
    return any(secret and secret in text for secret in secret_values)


class Capture:
    def __init__(self, request, provider, secret_values, network):
        self.request, self.provider, self.secret_values, self.network = request, provider, secret_values, network
        self.opens = 0
        self.safe = {"http_status": None, "provider_status": None, "reported_model": None, "usage": {}, "output_text": None, "network_attempted": False}

    def factory(self, *_):
        capture = self
        opener = _REAL_BUILD_OPENER(urllib.request.ProxyHandler({}), self.provider.NoNewsProviderRedirect())
        class BoundOpener:
            def open(self, req, timeout):
                if (capture.opens or req.full_url != capture.provider.ENDPOINTS[capture.request["provider"]]
                        or req.get_method() != "POST" or timeout != 45
                        or req.data != capture.provider.encode_body(capture.request["body"])):
                    raise Refused("transport_binding_mismatch")
                capture.opens += 1
                capture.safe["network_attempted"] = True
                capture.safe["sent_at"] = utc()
                capture.network["allowed"] = True
                try:
                    response = opener.open(req, timeout=timeout)
                except urllib.error.HTTPError as exc:
                    capture.safe["http_status"] = exc.code if type(exc.code) is int else None
                    raise
                finally:
                    capture.network["allowed"] = False
                capture.safe["http_status"] = response.status if type(response.status) is int else None
                class Response:
                    status = response.status
                    def geturl(self): return response.geturl()
                    def __enter__(self): return self
                    def __exit__(self, *_): response.close()
                    def read(self, size):
                        capture.network["allowed"] = True
                        try: raw = response.read(size)
                        finally: capture.network["allowed"] = False
                        capture.capture(raw)
                        return raw
                return Response()
        return BoundOpener()

    def capture(self, raw):
        if not isinstance(raw, bytes) or len(raw) > self.provider.MAX_RESPONSE_BYTES:
            return
        self.safe["response_sha256"] = sha(raw)
        try: payload = json.loads(raw.decode("utf-8"))
        except Exception: return
        if not isinstance(payload, dict): return
        self.safe["provider_status"] = payload.get("status") if payload.get("status") in {"completed", "incomplete", "failed", "in_progress"} else None
        self.safe["reported_model"] = self.provider.safe_reported_model(payload.get("model"), self.request["provider"], self.request["model"])
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self.safe["usage"] = {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens") if type(usage.get(k)) is int and 0 <= usage[k] <= 10_000_000}
            for parent, field in (("input_tokens_details", "cached_tokens"), ("output_tokens_details", "reasoning_tokens")):
                detail = usage.get(parent)
                if isinstance(detail, dict) and type(detail.get(field)) is int and 0 <= detail[field] <= 10_000_000:
                    self.safe["usage"][field] = detail[field]
        chunks = []
        for message in payload.get("output", []) if isinstance(payload.get("output"), list) else []:
            if not isinstance(message, dict) or message.get("type") != "message" or message.get("role") != "assistant": continue
            for part in message.get("content", []) if isinstance(message.get("content"), list) else []:
                if isinstance(part, dict) and part.get("type") == "output_text" and type(part.get("text")) is str:
                    chunks.append(part["text"])
        text = "".join(chunks)
        if text and len(text.encode("utf-8")) <= 65536 and not contains_secret(text, self.secret_values):
            self.safe["output_text"] = text


def run_one(root, request, provider, quality, key, ledger, network, secret_values=None):
    ledger.reserve(request)
    marker = root / "evidence" / (str(request["slot"]).zfill(2) + "-spent.json")
    write_new(marker, {"slot": request["slot"], "reserved_micro_cny": request["reserved_micro_cny"], "at": utc(), "body_sha256": request["body_sha256"]})
    secret_values = tuple(secret_values or (key,))
    capture = Capture(request, provider, secret_values, network)
    previous = provider.urllib.request.build_opener
    start = time.monotonic()
    receipt = {key: request[key] for key in ("slot", "sample_index", "sample_id", "provider", "model", "reserved_micro_cny", "body_sha256")}
    receipt.update(started_at=utc(), status="unknown", error_code=None, semantic_review_required=True)
    try:
        provider.urllib.request.build_opener = capture.factory
        response = provider.generate_response(request["provider"], request["model"], key, request["body"])
        if response.ok:
            if contains_secret(response.content, secret_values):
                raise Refused("credential_echo_rejected")
            try:
                validated = quality.validate_output(response.content, request["material"])
                if contains_secret(validated, secret_values):
                    raise Refused("credential_echo_rejected")
                receipt["validated_result"] = validated
                receipt["status"] = "completed"
            except ValueError as exc:
                code = str(exc)
                receipt["error_code"] = code if code in {"analysis_unsupported_relationship_direction", "duplicate_reading_field", "invalid_analysis", "invalid_analysis_evidence", "analysis_quote_not_in_source", "analysis_context_only_evidence"} else "quality_validation_failed"
                receipt["status"] = "quality_rejected"
        else:
            receipt["error_code"] = response.error_code if response.error_code in {"model_mismatch", "http_error", "invalid_or_unconfirmed_response"} else "unconfirmed_response"
            receipt["status"] = "failed" if capture.safe["http_status"] is not None else "unknown"
    except Exception:
        receipt["error_code"] = "execution_unconfirmed"
    finally:
        provider.urllib.request.build_opener = previous
        network["allowed"] = False
    receipt.update(capture.safe)
    receipt.update(finished_at=utc(), elapsed_seconds=round(time.monotonic() - start, 6), supplier_bill_verified=False)
    write_new(root / "evidence" / (str(request["slot"]).zfill(2) + "-result.json"), receipt)
    ledger.finish(request, receipt)
    capture.secret_values = ()
    return receipt


def install_guard(root, network):
    database = str(root / "comparison.sqlite3")
    def audit(event, args):
        if event == "sqlite3.connect" and str(args[0]) != database:
            raise Refused("foreign_database_forbidden")
        if event in {"socket.connect", "socket.getaddrinfo"} and not network["allowed"]:
            raise Refused("unapproved_network_forbidden")
        if event in {"subprocess.Popen", "os.system"}:
            raise Refused("child_process_forbidden")
        if event == "open" and isinstance(args[0], (str, bytes)):
            p = Path(os.fsdecode(args[0]))
            if p.name == ".env" or p.name.startswith(".env."):
                raise Refused("dotenv_forbidden")
            mode = args[1] or ""
            flags = args[2]
            writing = any(c in str(mode) for c in "wax+") or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and not p.resolve().is_relative_to(root):
                raise Refused("foreign_write_forbidden")
    sys.addaudithook(audit)


def execute(plan_path, expected, auth_path, auth_sha, allow):
    if not allow:
        raise Refused("explicit_real_flag_required")
    root, plan, requests, provider, quality = inspect_plan(plan_path, expected)
    auth = read_json(auth_path, auth_sha)
    check_authorization(auth, plan, expected)
    if any((root / n).exists() for n in ("started.json", "comparison.sqlite3", "terminal.json")):
        raise Refused("plan_already_started_no_replay")
    write_new(root / "started.json", {"plan_sha256": expected, "authorization_sha256": auth_sha, "at": utc(), "status": "claimed_no_replay"})
    credentials = module(HERE / "credentials.py", "comparison_credentials")
    network = {"allowed": False}
    install_guard(root, network)
    keys = {}
    receipts = []
    skipped = []
    terminal_status = "unknown"
    try:
        keys = credentials.ask_keys(timestamp(auth["expires_at"]))
        ledger = Ledger(root / "comparison.sqlite3")
        blocked = set()
        def work(cancel, progress):
            for request in requests:
                reason = ("user_cancelled" if cancel.is_set() else "authorization_expired" if time.time() >= timestamp(auth["expires_at"]) else "provider_authorization_rejected" if request["provider"] in blocked else None)
                if reason:
                    skipped.append({"slot": request["slot"], "reason": reason})
                    continue
                progress("正在审核第 %d/5 条 · %s\n已完成 %d 次请求；不重试。" % (request["sample_index"], "豆包" if request["provider"] == "doubao" else "DeepSeek Flash", len(receipts)))
                receipt = run_one(root, request, provider, quality, keys[request["provider"]], ledger, network, tuple(keys.values()))
                receipts.append(receipt)
                if receipt["http_status"] in {401, 403}:
                    blocked.add(request["provider"])
            return "finished"
        credentials.run_with_progress(work)
        terminal_status = "completed_all_slots" if not skipped else "completed_with_unattempted_slots"
    except credentials.PromptCancelled:
        terminal_status = "cancelled_before_requests"
    except Exception:
        terminal_status = "unconfirmed_stopped_no_retry"
    finally:
        keys.clear()
        network["allowed"] = False
    terminal = {"status": terminal_status, "at": utc(), "plan_sha256": expected, "recorded_results": len(receipts),
                "network_attempts": sum(r["network_attempted"] for r in receipts), "skipped": skipped,
                "recorded_reserved_micro_cny": sum(r["reserved_micro_cny"] for r in receipts),
                "incomplete_ledger_entries_may_remain_spent": terminal_status == "unconfirmed_stopped_no_retry",
                "production_changed": False, "retries": 0, "supplier_bill_verified": False}
    write_new(root / "terminal.json", terminal)
    return terminal


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--samples", required=True)
    prep.add_argument("--samples-sha256", required=True)
    prep.add_argument("--out", required=True)
    prep.add_argument("--checked-at")
    for name in ("inspect", "execute"):
        item = commands.add_parser(name)
        item.add_argument("--plan", required=True)
        item.add_argument("--plan-sha256", required=True)
        if name == "execute":
            item.add_argument("--authorization", required=True)
            item.add_argument("--authorization-sha256", required=True)
            item.add_argument("--allow-real-provider", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.samples, args.samples_sha256, args.out, args.checked_at)
        elif args.command == "inspect":
            root, plan, *_ = inspect_plan(args.plan, args.plan_sha256)
            result = {"status": "verified", "started": (root / "started.json").exists(), "requests": len(plan["requests"]), "reserved_micro_cny": plan["total_reserved_micro_cny"]}
        else:
            result = execute(args.plan, args.plan_sha256, args.authorization, args.authorization_sha256, args.allow_real_provider)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        print('{"status":"refused_or_unconfirmed","retry_permitted":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
