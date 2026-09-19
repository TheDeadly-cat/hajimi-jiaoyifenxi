"""Prepare or explicitly execute one bounded standard review in a manual trial."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import warnings
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
KEYS = ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY", "ARK_API_KEY",
        "DOUBAO_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY", "ZHIPUAI_API_KEY", "BAILIAN_TOKEN_PLAN_API_KEY")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--execute", action="store_true")
    parser.add_argument("--approve-plan-sha256", default="")
    parser.add_argument("--password-dialog", action="store_true")
    args = parser.parse_args(argv)
    for name in KEYS:
        os.environ.pop(name, None)
    os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    # Configuration modules capture paths at import time. Pin the isolated
    # destination before importing any backend service; validation below still
    # runs before ownership, SQLite startup, or credential input.
    requested_database = Path(config["database_path"]).expanduser().resolve()
    os.environ["AI_STUDIO_DATABASE_PATH"] = str(requested_database)
    os.environ["AI_STUDIO_RUNTIME_DIR"] = str(requested_database.parent.parent / "runtime")
    os.environ["AI_STUDIO_DISABLED_PROVIDERS"] = "openai,deepseek,qwen,glm"
    sys.path.insert(0, str(APP))
    from backend.controlled_manual_review import (
        ControlledManualReview, ControlledReviewError, DISABLED,
        isolated_review_database, verify_candidate,
    )
    from backend.api_pilot import write_record
    database = isolated_review_database(config["database_path"])
    root = database.parent.parent
    output = Path(args.output).resolve()
    if not output.is_relative_to(root) or output.suffix != ".json" or output.exists():
        raise ControlledReviewError("输出须为独立手工副本内一个尚不存在的 JSON 文件")
    if args.password_dialog and not args.execute:
        raise ControlledReviewError("密码框仅用于已批准的执行")
    verify_candidate(config["candidate_sha"])
    os.environ["AI_STUDIO_DISABLED_PROVIDERS"] = ",".join(sorted(DISABLED))
    os.environ["AI_STUDIO_DATABASE_PATH"] = str(database)
    os.environ["AI_STUDIO_RUNTIME_DIR"] = str(root / "runtime")
    from backend.database_migration import assert_database_ready_for_startup
    from backend.instance_ownership import DatabaseInstanceOwner
    from backend.providers.registry import ProviderRegistry
    from backend.store import STORE
    with DatabaseInstanceOwner(database) as owner:
        readiness = assert_database_ready_for_startup(database)
        STORE.configure_verified_startup(database, readiness["startup_identity"])
        store = STORE._resolve()
        try:
            registry = ProviderRegistry(disabled_provider_ids=DISABLED)
            control = ControlledManualReview(store, registry, instance_owner=owner)
            plan = control.prepare(config)
            if args.prepare:
                result = plan
            else:
                if not args.approve_plan_sha256 or args.approve_plan_sha256 != plan["plan_sha256"]:
                    raise ControlledReviewError("执行前必须明确批准当前计划哈希；未读取密钥")
                control._window(plan["config"])
                if args.password_dialog:
                    from scripts.pilot_password_dialog import ask_api_key
                    key = ask_api_key(plan, root / "review-key-input-status").strip()
                else:
                    if not sys.stdin.isatty():
                        raise ControlledReviewError("密钥仅接受本机密码框或可隐藏输入的终端")
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", getpass.GetPassWarning)
                        key = getpass.getpass("ARK_API_KEY（输入隐藏，不写文件）: ").strip()
                from scripts.pilot_password_dialog import acceptable_password
                if not acceptable_password(key):
                    raise ControlledReviewError("密钥为空或包含无效字符；未调用")
                registry = ProviderRegistry(disabled_provider_ids=DISABLED, api_keys={"doubao": key})
                key = ""
                result = ControlledManualReview(store, registry, instance_owner=owner).run(
                    config, approved_plan_sha256=args.approve_plan_sha256,
                )
            write_record(output, result)
            success = not args.execute or (
                result["session"]["state"] == "READY_FOR_DECISION"
                and len(result["receipts"]) == 3
                and all(r["status"] == "RESPONDED" for r in result["receipts"])
            )
            print(json.dumps({"ok": success, "mode": "execute" if args.execute else "prepare",
                              "output": str(output), "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                              "plan_sha256": plan["plan_sha256"], "new_http_attempts": result.get("new_http_attempts", 0),
                              "state": result.get("session", {}).get("state"), "supplier_bill_verified": False}, ensure_ascii=False))
        finally:
            for name in KEYS:
                os.environ.pop(name, None)
            store.checkpoint_after_shutdown(instance_owner=owner)
    return 0 if success else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('{"ok":false,"error":"本机操作已取消；请核对保留的账本，不会自动重试"}')
        raise SystemExit(130)
    except Exception as exc:
        safe = str(exc) if type(exc).__name__ in {"ControlledReviewError", "PilotError"} else "受控复核未完成；请核对配置、授权及本机回执，不会自动重试"
        print(json.dumps({"ok": False, "error": safe}, ensure_ascii=False))
        raise SystemExit(1)
