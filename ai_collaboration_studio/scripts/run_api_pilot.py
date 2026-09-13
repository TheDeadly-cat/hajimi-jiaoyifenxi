"""Operator-only pilot; defaults to evidence export and never reads local .env."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import warnings

APP = Path(__file__).resolve().parents[1]
KEYS = ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ARK_API_KEY", "DOUBAO_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY", "ZHIPU_API_KEY")
KEY_NAME = {"openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "doubao": "ARK_API_KEY", "glm": "GLM_API_KEY"}


class PilotCLIError(ValueError):
    pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--execute", action="store_true")
    parser.add_argument("--approve-plan-sha256", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    for name in KEYS:
        os.environ.pop(name, None)
    os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    head = subprocess.check_output(["git", "-C", str(APP), "rev-parse", "HEAD"], text=True).strip()
    if config.get("candidate_sha") != head or subprocess.check_output(["git", "-C", str(APP), "status", "--porcelain"], text=True).strip():
        raise PilotCLIError("候选必须与已提交的干净源码一致")
    database = Path(config["database_path"]).expanduser()
    root = database.parent.parent
    if database.name != "studio.sqlite3" or database.parent.name != "data" or not root.name.startswith("APIPilot"):
        raise PilotCLIError("只允许 APIPilot 独立目录中的 data/studio.sqlite3")
    output = Path(args.output).expanduser()
    if not output.resolve().is_relative_to(root.resolve()) or output.resolve() == database.resolve():
        raise PilotCLIError("输出必须位于独立试验目录且不能覆盖数据库")
    provider_id = config.get("provider", "")
    if (args.prepare or args.execute) and provider_id not in KEY_NAME:
        raise PilotCLIError("先明确选定 Provider 和模型")
    disabled = set(KEY_NAME).difference({provider_id}) if provider_id else set(KEY_NAME)
    os.environ["AI_STUDIO_DISABLED_PROVIDERS"] = ",".join(sorted(disabled))
    os.environ["AI_STUDIO_RUNTIME_DIR"] = str(root / "runtime")
    os.environ["AI_STUDIO_DATABASE_PATH"] = str(database)
    sys.path.insert(0, str(APP))
    from backend.api_pilot import ControlledAPIPilot, prepare_evidence, write_record
    from backend.database_migration import assert_database_ready_for_startup
    from backend.instance_ownership import DatabaseInstanceOwner
    from backend.providers.registry import ProviderRegistry
    from backend.store import STORE

    with DatabaseInstanceOwner(database) as owner:
        readiness = assert_database_ready_for_startup(database)
        STORE.configure_verified_startup(database, readiness["startup_identity"])
        store = STORE._resolve()
        try:
            if not args.prepare and not args.execute:
                if set(config) != {"candidate_sha", "database_path", "room_id", "session_id"}:
                    raise PilotCLIError("仅导出证据时配置只能包含候选、数据库、房间和任务ID")
                evidence = prepare_evidence(store, config["room_id"], config["session_id"])
                result = {"version": "api_pilot_evidence_preparation_v1", "candidate_sha": head,
                          "configured_model": None, "paid_authorization": False, "source": evidence}
            else:
                registry = ProviderRegistry(disabled_provider_ids=disabled)
                pilot = ControlledAPIPilot(store, registry, instance_owner=owner)
                plan = pilot.prepare(config)
                if args.prepare:
                    result = plan
                else:
                    if not args.approve_plan_sha256 or args.approve_plan_sha256 != plan["plan_sha256"]:
                        raise PilotCLIError("执行前必须明确批准当前计划哈希；未读取密钥")
                    if not sys.stdin.isatty():
                        raise PilotCLIError("密钥只接受可隐藏输入的交互终端")
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", getpass.GetPassWarning)
                        key = getpass.getpass(f"{KEY_NAME[provider_id]}（输入隐藏，不写文件）: ").strip()
                    if not key:
                        raise PilotCLIError("密钥为空；未调用")
                    os.environ[KEY_NAME[provider_id]] = key
                    registry = ProviderRegistry(disabled_provider_ids=disabled, api_keys={provider_id: key})
                    key = ""
                    result = ControlledAPIPilot(store, registry, instance_owner=owner).run(config, approved_plan_sha256=args.approve_plan_sha256)
            if args.execute and result.get("replayed") and output.exists():
                previous = json.loads(output.read_text(encoding="utf-8"))
                if previous.get("response") != result.get("response") or previous.get("run", {}).get("id") != result.get("run", {}).get("id"):
                    raise PilotCLIError("既有输出不是本次请求记录，请使用新的输出文件名")
            else:
                write_record(output, result)
            response = result.get("response") or {}
            accepted = (not args.execute or (response.get("status") == "RESPONDED" and response.get("usage_status") == "reported" and not result.get("outcome_unknown")))
            print(json.dumps({"ok": accepted, "mode": "execute" if args.execute else "prepare" if args.prepare else "evidence_only",
                              "output": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                              "plan_sha256": result.get("plan_sha256"), "replayed": result.get("replayed"),
                              "call_status": response.get("status"), "usage_status": response.get("usage_status"),
                              "supplier_bill_verified": response.get("supplier_bill_verified")}, ensure_ascii=False))
        finally:
            for name in KEYS:
                os.environ.pop(name, None)
            store.checkpoint_after_shutdown(instance_owner=owner)
    return 0 if accepted else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never dump config, Provider internals, response bodies or credentials.
        safe_message = str(exc) if isinstance(exc, PilotCLIError) or type(exc).__module__ == "backend.api_pilot" else "试验未完成，请检查本地配置、授权或保留的回执；未自动重试。"
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "message": safe_message}, ensure_ascii=False))
        raise SystemExit(1)
