"""Focused, offline acceptance of an already bootstrapped clean checkout.

No dependency installation, live-source request, production-port probe, broad
smoke suite, or production-process operation is performed here. All generated
data, archives and safe receipts remain in a new --work-root directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import news_portable_runtime as portable


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise portable.PortableError("verification_redirect_refused")


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return portable.validate_port(listener.getsockname()[1])


def get_local(port, path):
    portable.validate_port(port)
    if path not in {"/", "/api/readiness", "/api/version", "/api/news/overview"} and not re.fullmatch(r"/assets/[A-Za-z0-9_.-]+\.(js|css)", path):
        raise portable.PortableError("verification_path_not_allowed")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        if response.status != 200:
            raise portable.PortableError("verification_http_status")
        body = response.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise portable.PortableError("verification_response_too_large")
        return body


def stop_owned_child(child):
    """Use only the Popen object/OS handle created by this verifier, never PID lookup."""
    forced = False
    if child.poll() is None:
        try:
            child.stdin.write("stop\n")
            child.stdin.flush()
        except (OSError, BrokenPipeError):
            pass
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            forced = True
            child.terminate()
            child.wait(timeout=10)
    if child.stdin:
        child.stdin.close()
    return {"exit_code": child.returncode, "forced": forced, "scope": "owned_popen_handle_only"}


def verify_payloads(readiness, version, overview, expected_backend):
    if readiness.get("ready") is not True or readiness.get("ok") is not True:
        raise portable.PortableError("host_not_ready")
    if version.get("backend_build", {}).get("source_sha256") != expected_backend:
        raise portable.PortableError("loaded_backend_identity_mismatch")
    news = overview.get("overview", {})
    ai = news.get("ai", {})
    if (news.get("enabled") is not False or news.get("running") is not True
            or ai.get("configured") is not False or ai.get("busy") is not False
            or type(ai.get("calls_reserved")) is not int or ai["calls_reserved"] != 0):
        raise portable.PortableError("fresh_workspace_safe_defaults_mismatch")
    if news.get("counts", {}).get("total") != 0 or news.get("recent_checks"):
        raise portable.PortableError("fresh_workspace_contains_unexpected_checks_or_items")
    return {"host_ready": True, "backend_sha256": expected_backend,
            "collector_enabled": False, "collector_thread_alive": True,
            "ai": {name: ai[name] for name in ("configured", "busy", "calls_reserved")},
            "fresh_item_count": 0, "completed_source_checks": 0,
            "source_recovery_verified": False, "continuous_stability_verified": False}


def family_digests(root):
    result = {}
    for name in portable.DATABASE_NAMES:
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = portable.unaliased_path(root / (name + suffix))
            result[name + suffix] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return result


def synthetic_release_roundtrip(project, work, data):
    from scripts import create_versioned_source_backup as backup
    from scripts import run_isolated_release_drill as release
    from scripts.run_fresh_source_smoke import safe_extract

    before = family_digests(data)
    timestamp = datetime.now(timezone.utc).replace(microsecond=0)
    candidate_archive = backup.create_backup(source_root=project, destination_root=work / "candidate-archive",
        source_root_label="news_complete_candidate", created_at_utc=timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"))
    baseline = work / "synthetic-baseline-source"
    safe_extract(candidate_archive, baseline)
    # This manifest belongs to the just-created extraction, never a prior report.
    (baseline / backup.MANIFEST_NAME).unlink()
    readme = baseline / "README.md"
    with readme.open("a", encoding="utf-8") as stream:
        stream.write("\n<!-- Synthetic README-only rollback baseline; not a production release. -->\n")
    baseline_archive = backup.create_backup(source_root=baseline, destination_root=work / "baseline-archive",
        source_root_label="news_synthetic_baseline", created_at_utc=(timestamp-timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    root = work / "release-root"
    old = release.install_release(baseline_archive, root)
    new = release.install_release(candidate_archive, root)
    first = release.activate_release(root, old["release_id"], expected_active_release_id=None)
    upgraded = release.activate_release(root, new["release_id"], expected_active_release_id=old["release_id"])
    failure = release.build_synthetic_failure_receipt(new["release_id"])
    restored = release.rollback_release(root, failed_release_id=new["release_id"], target_release_id=old["release_id"],
        expected_generation=upgraded["generation"], failure_receipt=failure)
    unchanged = before == family_digests(data)
    if not unchanged or restored["active_release_id"] != old["release_id"] or restored["generation"] != 3:
        raise portable.PortableError("synthetic_release_roundtrip_failed")
    return {"ok": True, "synthetic": True, "production_rollback_proven": False,
            "baseline_difference": "README_only", "data_unchanged": unchanged,
            "candidate_release_id": new["release_id"], "baseline_release_id": old["release_id"],
            "candidate_source_sha256": new["source_total_sha256"],
            "generations": [first["generation"], upgraded["generation"], restored["generation"]]}


def focused_tests(python, project, work, environment):
    result_path = work / "focused-tests.json"
    code = ("import json,sys,unittest;from pathlib import Path;"
            "sys.path.insert(0,sys.argv[1]);"
            "s=unittest.defaultTestLoader.loadTestsFromName('tests.test_news_portable_delivery');"
            "r=unittest.TextTestRunner(verbosity=1).run(s);"
            "Path(sys.argv[2]).write_text(json.dumps({'tests':r.testsRun,'failures':len(r.failures),"
            "'errors':len(r.errors),'ok':r.wasSuccessful()}),encoding='utf-8');"
            "sys.exit(0 if r.wasSuccessful() else 1)")
    completed = subprocess.run([str(python), "-I", "-B", "-X", "utf8", "-c", code,
                                str(project), str(result_path)], cwd=project, env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=120, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {"ok": False}
    if completed.returncode != 0 or result.get("ok") is not True:
        raise portable.PortableError("focused_portable_tests_failed")
    return result


def verify(project, work, python, port, run_tests=False):
    project, work, python = (portable.unaliased_path(path) for path in (project, work, python))
    if not (project / "scripts/run_news_workspace.py").is_file() or not (project / "frontend/dist/index.html").is_file():
        raise portable.PortableError("bootstrapped_source_and_built_frontend_required")
    if not python.is_file():
        raise portable.PortableError("python_executable_missing")
    if work.exists() or work.is_relative_to(project):
        raise portable.PortableError("verification_requires_new_work_directory_outside_source")
    portable.validate_port(port)
    with portable.reserve_port(port):
        pass
    work.mkdir(parents=True, exist_ok=False)
    receipt = {"schema_version": "news_clean_checkout_verification_v1", "ok": False,
               "started_at": datetime.now(timezone.utc).isoformat(), "port": port,
               "production_port_probed": False, "paid_calls_authorized": False,
               "external_source_checks": False, "ci_rerun": False}
    child = None
    try:
        environment = portable.safe_environment()
        environment.update({"AI_STUDIO_RUNTIME_DIR": str(work / "data"),
                            "AI_STUDIO_DATABASE_PATH": str(work / "data" / portable.DATABASE_NAMES[0])})
        expected = portable.backend_identity(project)
        child = subprocess.Popen([str(python), "-I", "-B", "-X", "utf8",
                                  str(project / "scripts/run_news_workspace.py"),
                                  "--data-root", str(work / "data"), "--port", str(port),
                                  "--initialize-new", "--offline", "--controlled-stdin",
                                  "--receipt", str(work / "child-receipt.json")],
                                 cwd=project, env=environment, stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        receipt["owned_child_pid"] = child.pid
        deadline = time.monotonic() + 60
        readiness = None
        while child.poll() is None and time.monotonic() < deadline:
            try:
                readiness = json.loads(get_local(port, "/api/readiness"))
                if readiness.get("ready") is True:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if readiness is None or readiness.get("ready") is not True or child.poll() is not None:
            raise portable.PortableError("isolated_host_startup_not_ready")
        version = json.loads(get_local(port, "/api/version"))
        overview = json.loads(get_local(port, "/api/news/overview"))
        receipt["service"] = verify_payloads(readiness, version, overview, expected)
        index = get_local(port, "/")
        index_hash = hashlib.sha256(index).hexdigest()
        if index_hash != hashlib.sha256((project / "frontend/dist/index.html").read_bytes()).hexdigest():
            raise portable.PortableError("served_frontend_identity_mismatch")
        scripts = re.findall(r'<script\b[^>]*\bsrc="(/assets/[A-Za-z0-9_.-]+\.js)"', index.decode("utf-8"))
        if not scripts:
            raise portable.PortableError("built_frontend_entry_missing")
        asset_hash = hashlib.sha256(get_local(port, scripts[0])).hexdigest()
        if asset_hash != hashlib.sha256((project / "frontend/dist" / scripts[0].lstrip("/")).read_bytes()).hexdigest():
            raise portable.PortableError("served_frontend_asset_mismatch")
        receipt["frontend"] = {"index_sha256": index_hash, "entry_asset_sha256": asset_hash, "served": True}
        receipt["child_stop"] = stop_owned_child(child)
        child = None
        if receipt["child_stop"]["forced"] or receipt["child_stop"]["exit_code"] != 0:
            raise portable.PortableError("isolated_child_did_not_exit_normally")
        child_receipt = json.loads((work / "child-receipt.json").read_text(encoding="utf-8"))
        if child_receipt.get("ok") is not True or child_receipt.get("offline") is not True:
            raise portable.PortableError("child_offline_receipt_not_confirmed")
        receipt["rollback"] = synthetic_release_roundtrip(project, work, work / "data")
        receipt["tests"] = focused_tests(python, project, work, environment) if run_tests else {"run": False}
        receipt["ok"] = True
    except Exception as exc:
        receipt["error_type"] = type(exc).__name__
        if isinstance(exc, portable.PortableError):
            receipt["error_code"] = str(exc)
    finally:
        if child is not None:
            receipt["child_stop"] = stop_owned_child(child)
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        portable.write_receipt(work / "verification.json", receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--port", type=int)
    parser.add_argument("--run-focused-tests", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = verify(Path(args.project_root), Path(args.work_root), Path(args.python),
                        args.port if args.port is not None else available_port(), args.run_focused_tests)
    except Exception as exc:
        result = {"ok": False, "error_type": type(exc).__name__}
        if isinstance(exc, portable.PortableError):
            result["error_code"] = str(exc)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
