"""Initialize, prepare, or explicitly run one owned native news-review trial.

No mode widens sources, migrates an existing DB, resumes an expired policy, or
retries a paid request. Credentials are accepted only through local masked input.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
import threading
import time
import uuid
import warnings
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
KEYS = ("OPENAI_API_KEY","ARK_API_KEY","DOUBAO_API_KEY","DEEPSEEK_API_KEY","QWEN_API_KEY",
        "DASHSCOPE_API_KEY","GLM_API_KEY","ZHIPU_API_KEY","ZHIPUAI_API_KEY","BAILIAN_TOKEN_PLAN_API_KEY")
_RETAINED_OWNER = None


def isolated_path(value):
    from backend.news_review_contracts import require
    from backend.path_identity import first_reparse_component
    path = Path(value).expanduser()
    require(path.is_absolute() and first_reparse_component(path) is None, "invalid_trial_path")
    path = path.resolve()
    require(path.name == "studio.sqlite3" and path.parent.name == "data"
            and path.parent.parent.name.startswith("NewsReviewTrial")
            and not path.is_relative_to(APP.parent), "isolated_database_required")
    return path


def main(argv=None):
    global _RETAINED_OWNER
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--initialize",action="store_true")
    action.add_argument("--prepare",action="store_true")
    action.add_argument("--prepare-activation",action="store_true")
    action.add_argument("--activate",action="store_true")
    action.add_argument("--run",action="store_true")
    action.add_argument("--report",action="store_true")
    parser.add_argument("--root")
    parser.add_argument("--candidate-sha")
    parser.add_argument("--config")
    parser.add_argument("--output")
    parser.add_argument("--approve-policy-sha256",default="")
    parser.add_argument("--approve-activation-sha256",default="")
    parser.add_argument("--duration-ms",type=int,default=86_400_000)
    parser.add_argument("--resume-paused",action="store_true")
    parser.add_argument("--password-dialog",action="store_true")
    parser.add_argument("--sec-user-agent",default="")
    parser.add_argument("--port",type=int,default=0)
    args = parser.parse_args(argv)
    for key in KEYS:
        os.environ.pop(key,None)
    os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    os.environ["AI_STUDIO_DISABLED_PROVIDERS"] = "openai,deepseek,qwen,glm"
    executing = args.run or args.activate
    if executing:
        if ("@" not in args.sec_user_agent or not 1 <= len(args.sec_user_agent) <= 256
                or any(not 32 <= ord(c) < 127 for c in args.sec_user_agent)):
            parser.error("--run requires a public SEC product/contact User-Agent")
        os.environ["SEC_USER_AGENT"] = args.sec_user_agent
    sys.path.insert(0,str(APP))
    if args.initialize:
        if not args.root or not args.candidate_sha or args.config:
            parser.error("initialization requires --root and --candidate-sha, without --config")
        requested = Path(args.root)/"data"/"studio.sqlite3"
    else:
        if not args.config or args.root or args.candidate_sha:
            parser.error("prepare/run/report requires --config")
        policy = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        requested = Path(policy["database_path"])
    from backend.news_review_contracts import DISABLED, NewsReviewError, require, validate_policy
    # These pure path/contract checks precede config.py, whose directory setup is
    # intentional only after this destination has been admitted.
    database = isolated_path(requested)
    root = database.parent.parent
    require(args.port == 0 or 1024 <= args.port <= 65535 and args.port not in {8770,11111}, "invalid_port")
    require(not args.password_dialog or executing, "run_option_without_run")
    require(not (args.resume_paused or args.approve_policy_sha256) or args.run, "fixed_run_option_required")
    require(not args.approve_activation_sha256 or args.activate, "activation_option_without_activation")
    require(0 < args.duration_ms <= 86_400_000, "invalid_duration")
    if args.initialize:
        require(not root.exists(), "trial_already_exists")
        root.mkdir(parents=True,exist_ok=False)
        database.parent.mkdir()
    else:
        policy = validate_policy(policy)
        require(database.is_file(), "trial_not_initialized")
    # Bind captured backend configuration before importing store/provider modules.
    os.environ["AI_STUDIO_DATABASE_PATH"] = str(database)
    os.environ["AI_STUDIO_RUNTIME_DIR"] = str(root/"runtime")
    from backend.news_review_service import NewsReviewService
    from backend.controlled_manual_review import verify_candidate
    from backend.api_pilot import write_record
    from backend.instance_ownership import DatabaseInstanceOwner
    from backend.store import StudioStore, STORE
    from backend.providers.registry import ProviderRegistry
    if args.initialize:
        verify_candidate(args.candidate_sha)
        with DatabaseInstanceOwner(database) as owner:
            # Schema initialization remains confined to system temp. Publish
            # only this newly created, checkpointed image into an exclusive
            # destination; never relax StudioStore's formal-DB migration gate.
            with tempfile.TemporaryDirectory(prefix="news-review-initialize-") as temporary:
                seed_path = Path(temporary)/"studio.sqlite3"
                with DatabaseInstanceOwner(seed_path) as seed_owner:
                    seed = StudioStore(seed_path)
                    room = seed.create_room("自动消息审核试运行", "用于本次独立试运行的预算归属，不自动创建讨论轮次。")
                    seed.checkpoint_after_shutdown(instance_owner=seed_owner)
                    owner.assert_held_for(database)
                    with database.open("xb") as output:
                        output.write(seed_path.read_bytes())
                        output.flush()
                        os.fsync(output.fileno())
            from backend.database_migration import assert_database_ready_for_startup
            readiness = assert_database_ready_for_startup(database)
            result = {"database_path":str(database),"room_id":room["room"]["id"],
                      "candidate_sha":args.candidate_sha,"database_sha256":readiness["source_sha256"],
                      "network_started":False,"provider_calls":0}
            write_record(root/"initialized.json",result)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    policy = validate_policy(policy)
    require(database.is_file(), "trial_not_initialized")
    verify_candidate(policy["candidate_sha"])
    output = Path(args.output).resolve() if args.output else root/("report-"+uuid.uuid4().hex+".json")
    require(output.is_relative_to(root) and output.suffix == ".json" and not output.exists(), "invalid_output_path")
    owner = DatabaseInstanceOwner(database).acquire()
    retain_owner = False
    store = None
    watcher = None
    stop = threading.Event()
    controller = None
    exit_code = 0
    checkpoint_done = False
    try:
        from backend.database_migration import assert_database_ready_for_startup
        readiness = assert_database_ready_for_startup(database)
        STORE.configure_verified_startup(database,readiness["startup_identity"])
        store = STORE._resolve()
        providers = ProviderRegistry(disabled_provider_ids=DISABLED,api_keys={"doubao":""})
        service = NewsReviewService(store,providers,instance_owner=owner)
        preview = service.preview(policy)
        from backend.decision_lineage import canonical_sha256
        activation = {"version":"news_review_activation_v1","policy_template":policy,
                      "duration_ms":args.duration_ms,"time_resolution":"first_local_key_submission",
                      "permitted_changes":["not_before_ms","expires_at_ms"],"activations":1}
        activation_sha = canonical_sha256(activation)
        if args.prepare_activation:
            write_record(output,{"activation":activation,"activation_sha256":activation_sha,
                                 "supplier_bill_verified":False})
        elif args.prepare:
            write_record(output,preview)
        elif args.report:
            from backend.news_review_report import build_news_review_report
            write_record(output,build_news_review_report(service,policy["policy_id"]))
        else:
            if args.activate:
                require(args.approve_activation_sha256 == activation_sha, "activation_approval_mismatch")
                require(not (root/"activation-policy.json").exists()
                        and not (root/"activation-receipt.json").exists(), "activation_already_reserved")
            else:
                require(args.approve_policy_sha256 == preview["policy_sha256"], "approval_mismatch")
            require(policy["not_before_ms"] <= service.clock() < policy["expires_at_ms"], "policy_expired")
            if args.password_dialog:
                from scripts.pilot_password_dialog import ask_api_key
                key_plan = {"config":{**policy,"pilot_id":policy["policy_id"],"spend_plan_limit":policy["spend_limit_cny"]},
                            "max_calls":policy["max_model_calls"],"plan_sha256":activation_sha if args.activate else preview["policy_sha256"]}
                key = ask_api_key(key_plan,root/"key-input-status").strip()
            else:
                require(sys.stdin.isatty(), "masked_local_input_required")
                with warnings.catch_warnings():
                    warnings.simplefilter("error",getpass.GetPassWarning)
                    key = getpass.getpass("ARK_API_KEY（本机隐藏输入，不保存）: ").strip()
            from scripts.pilot_password_dialog import acceptable_password
            require(acceptable_password(key), "invalid_credential_input")
            providers = ProviderRegistry(disabled_provider_ids=DISABLED,api_keys={"doubao":key})
            key = ""
            service = NewsReviewService(store,providers,instance_owner=owner)
            approved_hash = args.approve_policy_sha256
            if args.activate:
                from contextlib import closing
                with store._lock,closing(store._connect()) as db:
                    require(not db.execute("SELECT 1 FROM news_review_policies WHERE id=?",(policy["policy_id"],)).fetchone(),
                            "activation_already_reserved")
                activated_at = service.clock()
                require(policy["not_before_ms"] <= activated_at < policy["expires_at_ms"], "activation_expired")
                policy = validate_policy({**policy,"not_before_ms":activated_at,"expires_at_ms":activated_at+args.duration_ms})
                preview = service.preview(policy)
                approved_hash = preview["policy_sha256"]
                # Exclusive files consume this activation even if the process
                # dies before its DB approval. Resume uses the resolved policy;
                # activation can never slide the window or reset spent slots.
                write_record(root/"activation-policy.json",policy)
                write_record(root/"activation-receipt.json",{"activation":activation,"activation_sha256":activation_sha,
                    "policy_sha256":approved_hash,"activated_at":activated_at,"expires_at":policy["expires_at_ms"]})
            current = service.approve(policy,approved_policy_sha256=approved_hash)
            if args.resume_paused:
                current = service.resume(policy["policy_id"],approved_policy_sha256=args.approve_policy_sha256)
            require(current["state"] == "ACTIVE", "policy_not_active")
            from backend.news_review_controller import NewsReviewController
            from backend.news_review_report import NewsReviewJournal, build_news_review_report
            from backend.news_review_stop import NewsReviewStop
            from backend.source_monitoring.settings import SourceMonitoringSettings
            from backend.source_monitoring.runtime import build_source_monitoring_runtime
            from backend.source_monitoring.profiles import require_profile_registry
            from backend.http_server import RuntimeShutdownIncomplete, run_server
            controller = NewsReviewController(service,policy["policy_id"])
            journal = NewsReviewJournal(service,policy["policy_id"])
            termination = NewsReviewStop(service,policy,controller,journal,stop,root)
            holder = {}
            def factory(owned_store):
                settings = SourceMonitoringSettings(enabled=True,auto_start=True,dry_run=False,
                    source_profile=policy["profile"],official_only=True,allow_readonly_market=False,
                    initial_mode="seed_only",trading_impact_rules_enabled=False)
                runtime = build_source_monitoring_runtime(owned_store,settings,
                    poll_authorization=controller.source_authorization,cycle_observer=journal.observe_source_cycle)
                require_profile_registry(runtime.scheduler.registry,policy["profile"])
                service.prepare_sources(policy["policy_id"],runtime)
                holder["runtime"] = runtime
                return runtime
            def ready(server):
                write_record(root/("host-"+journal.session_id+".json"),{"url":f"http://127.0.0.1:{server.server_port}",
                             "policy_sha256":preview["policy_sha256"],"expires_at_ms":policy["expires_at_ms"]})
                holder["ready"] = True
            journal.record("session_started",runtime_status="starting")
            watcher = threading.Thread(target=termination.watch,args=(holder,),name="news-review-observer",daemon=False)
            watcher.start()
            try:
                run_server(host="127.0.0.1",port=args.port,instance_owner=owner,runtime_factory=factory,
                           news_review_controller=controller,stop_event=stop,ready_callback=ready,
                           news_review_shutdown_seconds=255)
            except BaseException as exc:
                termination.request('operator_interrupted' if isinstance(exc,KeyboardInterrupt) else 'host_failure',
                    trigger='host',code=exc.code if isinstance(exc,NewsReviewError) else 'host_failed',
                    runtime_status='unavailable',exception_type=type(exc).__name__)
                # Unexpected host errors may interrupt handler/worker draining.
                # Keep the owner for every uncertain exit, not only a known
                # timeout exception; process exit releases it after threads end.
                retain_owner = True
                _RETAINED_OWNER = owner
                raise
            finally:
                stop.set()
                watcher.join(15)
                if watcher.is_alive():
                    retain_owner = True
                    _RETAINED_OWNER = owner
                    # The observer may itself be blocked writing a receipt.
                    # Do not acquire its lock or claim clean shutdown here.
                    write_record(root/('observer-drain-'+journal.session_id+'.json'),
                        {'ok':False,'code':'observer_shutdown_incomplete','session_id':journal.session_id})
            if not retain_owner and not termination.events:
                termination.request('unexpected_host_return',trigger='host',code='host_returned_before_stop',runtime_status='stopped')
            if not retain_owner:
                for lane,code in controller.errors.copy().items():
                    if lane != 'observer':
                        termination.request('worker_failure',trigger=lane,code=code,runtime_status='stopped')
            exit_code = termination.summary()['exit_code'] if not retain_owner else 2
            if not retain_owner:
                stage = 'stop_journal_failed'
                try:
                    journal.record("session_stopped",runtime_status="stopped")
                    stage = 'report_build_failed'
                    report = build_news_review_report(service,policy["policy_id"])
                    report['termination'] = termination.summary()
                    stage = 'checkpoint_failed'
                    store.checkpoint_after_shutdown(instance_owner=owner)
                    checkpoint_done = True
                    stage = 'report_write_failed'
                    write_record(output,report)
                except BaseException as exc:
                    termination.request('finalization_failure',trigger='host',code=stage,
                                        runtime_status='stopped',exception_type=type(exc).__name__)
                    retain_owner = True
                    _RETAINED_OWNER = owner
                    raise
        print(json.dumps({"ok":exit_code == 0,"exit_code":exit_code,"output":str(output),"report_written":output.is_file(),"policy_sha256":preview["policy_sha256"],
                          "activation_sha256":activation_sha if args.activate or args.prepare_activation else None,
                          "supplier_bill_verified":False},ensure_ascii=False))
        return exit_code
    finally:
        stop.set()
        for key in KEYS:
            os.environ.pop(key,None)
        if not retain_owner:
            if store is not None and not checkpoint_done:
                store.checkpoint_after_shutdown(instance_owner=owner)
            owner.release()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('{"ok":false,"code":"operator_interrupted","message":"已中断；保留账本，不自动重发。"}')
        raise SystemExit(130)
    except Exception as exc:
        # Never serialize exception text, input config, provider responses or keys.
        code = getattr(exc,"code","news_trial_failed")
        print(json.dumps({"ok":False,"code":code,"message":"试运行未完成，请核对本机记录。"},ensure_ascii=False))
        raise SystemExit(1)
