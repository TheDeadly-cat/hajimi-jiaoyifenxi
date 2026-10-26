"""Start the portable news workspace with explicit, preflighted data ownership.

First run: --initialize-new. Later runs: omit it. Existing data is never
migrated or recovered by this entry. Use the documented explicit migration
workflow when preflight refuses it.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext, closing
from datetime import datetime, timezone
import _thread
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import urllib.request
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from scripts import news_portable_runtime as portable


def initialize_new_databases(root):
    """Build a fresh synthetic image under temp, then publish without overwrite.

    StudioStore intentionally permits initialization only under system temp.
    No production schema-initialization exception is added to that policy.
    """
    from backend.store import StudioStore
    from backend.news_platform import NewsPlatform
    from backend.news_platform_ai import NewsAnalysis

    with tempfile.TemporaryDirectory(prefix="studio-new-news-data-") as temporary:
        staging = Path(temporary)
        StudioStore(staging / portable.DATABASE_NAMES[0])
        news = NewsPlatform(staging / portable.DATABASE_NAMES[1])
        NewsAnalysis(staging / portable.DATABASE_NAMES[2], news)
        for name in portable.DATABASE_NAMES:
            source = staging / name
            with closing(sqlite3.connect(source)) as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("PRAGMA journal_mode=DELETE")
            with source.open("rb") as reader, (root / name).open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())


def preflight(root):
    from backend.database_migration import assert_database_ready_for_startup
    from backend.database_startup_recovery import assert_no_pending_startup_recovery

    database = portable.closed_database(root / portable.DATABASE_NAMES[0])
    assert_no_pending_startup_recovery(database)
    news = portable.preflight_news_schema(root / portable.DATABASE_NAMES[1])
    analysis = portable.preflight_news_schema(root / portable.DATABASE_NAMES[2], analysis=True)
    main = assert_database_ready_for_startup(database)
    return main, {"studio_schema_ready": True, "news": news, "analysis": analysis,
                  "implicit_migration": False, "implicit_recovery": False}


def _controlled_stop():
    # Only the parent that created this stdin pipe can send this message.
    if sys.stdin.readline().strip() == "stop":
        _thread.interrupt_main()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root")
    parser.add_argument("--port", type=int, default=portable.DEFAULT_PORT)
    parser.add_argument("--initialize-new", action="store_true")
    parser.add_argument("--offline", action="store_true", help="Block all outgoing socket connections and child processes")
    parser.add_argument("--controlled-stdin", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--receipt", help="New safe receipt file; defaults to runtime/news-launcher/receipts")
    args = parser.parse_args(argv)
    receipt = {"schema_version": "news_portable_start_v1", "pid": os.getpid(),
               "started_at": datetime.now(timezone.utc).isoformat(), "offline": args.offline,
               "initialized_new": False, "port": args.port, "ok": False}
    owner = reservation = None
    receipt_path = PROJECT / "runtime/news-launcher/receipts" / (uuid.uuid4().hex + "-startup.json")
    try:
        if args.receipt:
            requested_receipt = portable.unaliased_path(args.receipt)
            if requested_receipt.exists():
                raise portable.PortableError("receipt_already_exists")
            receipt_path = requested_receipt
        root = portable.data_root(PROJECT, args.data_root)
        if args.initialize_new:
            portable.require_empty_data(root)
        else:
            for name in portable.DATABASE_NAMES:
                portable.closed_database(root / name)
        reservation = portable.reserve_port(args.port)
        proxy_view = None
        if not args.offline:
            try:
                # Use the standard effective view, including Windows registry
                # settings, before environment sanitization. Never log it.
                proxy_view = urllib.request.getproxies()
            except Exception:
                raise portable.PortableError("proxy_configuration_error") from None
        environment = (portable.safe_environment() if args.offline
                       else portable.safe_environment(feed_proxy_view=proxy_view))
        environment.update({"AI_STUDIO_RUNTIME_DIR": str(root),
                            "AI_STUDIO_PORTABLE_NEWS": "1",
                            "AI_STUDIO_DATABASE_PATH": str(root / portable.DATABASE_NAMES[0]),
                            "AI_STUDIO_HOST": "127.0.0.1", "AI_STUDIO_PORT": str(args.port)})
        os.environ.clear()
        os.environ.update(environment)
        with portable.offline_guard() if args.offline else nullcontext():
            from backend.instance_ownership import DatabaseInstanceOwner
            owner = DatabaseInstanceOwner(root / portable.DATABASE_NAMES[0])
            owner.acquire(metadata={"host": "127.0.0.1", "port": args.port, "entry": "portable_news"})
            if args.initialize_new:
                initialize_new_databases(root)
                receipt["initialized_new"] = True
            readiness, summary = preflight(root)
            receipt["preflight"] = summary
            from backend.store import STORE
            STORE.configure_verified_startup(root / portable.DATABASE_NAMES[0], readiness["startup_identity"])
            from backend.http_server import run_server
            receipt["backend_sha256"] = portable.backend_identity(PROJECT)
            # The host's own bind is authoritative if another process races this
            # hand-off. It fails without finding, stopping or reusing that process.
            reservation.close()
            reservation = None
            if args.controlled_stdin:
                threading.Thread(target=_controlled_stop, daemon=True).start()
            run_server(instance_owner=owner)
            receipt["ok"] = True
    except Exception as exc:
        receipt["error_type"] = type(exc).__name__
        if isinstance(exc, portable.PortableError):
            receipt["error_code"] = str(exc)
    finally:
        if reservation is not None:
            reservation.close()
        if owner is not None:
            owner.release()
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            portable.write_receipt(receipt_path, receipt)
        except Exception:
            receipt["ok"] = False
            print("启动诊断未能保存；请保留已有数据并通过欢迎窗口复制安全诊断。", file=sys.stderr)
    if not receipt["ok"]:
        print("工作台启动未完成，已有数据保持原状；请通过欢迎窗口查看安全诊断。", file=sys.stderr)
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
