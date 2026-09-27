"""Prepare or run one separately approved synthetic model-quality batch."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import warnings
from contextlib import closing

APP = Path(__file__).resolve().parents[1]
KEYS = ('OPENAI_API_KEY','ARK_API_KEY','DOUBAO_API_KEY','DEEPSEEK_API_KEY','QWEN_API_KEY',
        'DASHSCOPE_API_KEY','GLM_API_KEY','ZHIPU_API_KEY','ZHIPUAI_API_KEY','BAILIAN_TOKEN_PLAN_API_KEY')
_RETAINED_OWNER = None


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _database(value):
    from backend.path_identity import first_reparse_component
    path = Path(value)
    _require(path.is_absolute() and first_reparse_component(path) is None, 'quality_database_required')
    path = path.resolve()
    _require(path.name == 'studio.sqlite3' and path.parent.name == 'data'
        and path.parent.parent.name.startswith('NewsQualityBenchmark')
        and not path.is_relative_to(APP.parent), 'quality_database_required')
    return path


def main(argv=None):
    global _RETAINED_OWNER
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--initialize',action='store_true')
    action.add_argument('--prepare',action='store_true')
    action.add_argument('--run',action='store_true')
    parser.add_argument('--root')
    parser.add_argument('--candidate-sha')
    parser.add_argument('--database')
    parser.add_argument('--room-id')
    parser.add_argument('--references')
    parser.add_argument('--not-before-ms',type=int)
    parser.add_argument('--expires-at-ms',type=int)
    parser.add_argument('--output')
    parser.add_argument('--plan')
    parser.add_argument('--approve-plan-sha256')
    parser.add_argument('--acknowledge-human-references',action='store_true')
    parser.add_argument('--password-dialog',action='store_true')
    args = parser.parse_args(argv)
    for name in KEYS:
        os.environ.pop(name,None)
    os.environ['AI_STUDIO_SKIP_LOCAL_ENV'] = '1'
    os.environ['AI_STUDIO_DISABLED_PROVIDERS'] = 'openai,deepseek,qwen,glm'
    sys.path.insert(0,str(APP))
    _require(args.run or not (args.plan or args.approve_plan_sha256
        or args.acknowledge_human_references or args.password_dialog), 'quality_run_option_without_run')
    plan_raw = None
    if args.initialize:
        _require(args.root and args.candidate_sha and not any((args.database,args.room_id,args.references,
            args.not_before_ms,args.expires_at_ms,args.output)), 'quality_initialize_options')
        database = _database(Path(args.root)/'data'/'studio.sqlite3')
        candidate = args.candidate_sha
    elif args.prepare:
        _require(all((args.database,args.room_id,args.references,args.candidate_sha,args.output,
            args.not_before_ms,args.expires_at_ms)) and not args.root, 'quality_prepare_options')
        database, candidate = _database(args.database), args.candidate_sha
    else:
        _require(args.plan and args.references and not any((args.root,args.candidate_sha,args.database,
            args.room_id,args.not_before_ms,args.expires_at_ms,args.output)), 'quality_run_options')
        plan_raw = Path(args.plan).read_bytes()
        _require(len(plan_raw) <= 2000000, 'quality_plan_size_invalid')
        preliminary = json.loads(plan_raw)
        database, candidate = _database(preliminary['database_path']), preliminary['candidate_sha']
    root = database.parent.parent
    if args.initialize:
        _require(not root.exists(), 'quality_trial_already_exists')
        _require(subprocess.check_output(['git','-C',str(APP),'rev-parse','HEAD'],text=True).strip() == candidate
            and not subprocess.check_output(['git','-C',str(APP),'status','--porcelain'],text=True).strip(),
            'quality_candidate_not_exact_clean')
        root.mkdir(parents=True,exist_ok=False)
        database.parent.mkdir()
    else:
        _require(database.is_file(), 'quality_trial_not_initialized')
    os.environ['AI_STUDIO_DATABASE_PATH'] = str(database)
    os.environ['AI_STUDIO_RUNTIME_DIR'] = str(root/'runtime')
    from backend.controlled_manual_review import verify_candidate
    from backend.news_review_monitor import publish_json_once
    from backend.instance_ownership import DatabaseInstanceOwner
    from backend.news_review_quality_preparation import strict_json
    from backend.news_review_quality_ledger import QualityBatchLedger, build_batch_plan
    from backend.database_migration import assert_database_ready_for_startup
    from backend.store import StudioStore, STORE
    verify_candidate(candidate)
    if args.initialize:
        with DatabaseInstanceOwner(database) as owner:
            with tempfile.TemporaryDirectory(prefix='quality-initialize-') as directory:
                seed_path = Path(directory)/'studio.sqlite3'
                with DatabaseInstanceOwner(seed_path) as seed_owner:
                    seed = StudioStore(seed_path)
                    room = seed.create_room('合成材料模型质量测评','独立预约，不采集真实来源。')['room']['id']
                    seed.checkpoint_after_shutdown(instance_owner=seed_owner)
                    owner.assert_held_for(database)
                    with database.open('xb') as output:
                        output.write(seed_path.read_bytes())
                        output.flush()
                        os.fsync(output.fileno())
            readiness = assert_database_ready_for_startup(database)
            result = {'version':'news_quality_initialized_v1','candidate_sha':candidate,
                'database_path':str(database),'room_id':room,'database_sha256':readiness['source_sha256'],
                'provider_calls':0,'request_authorized':False}
            publish_json_once(root/'initialized.json',result)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    _require(database.is_file(), 'quality_trial_not_initialized')
    corpus = (APP/'tests/fixtures/news_review_quality_v1.json').read_bytes()
    references = Path(args.references).read_bytes()
    if args.prepare:
        plan = build_batch_plan(corpus,references,candidate_sha=candidate,database_path=database,
            room_id=args.room_id,not_before_ms=args.not_before_ms,expires_at_ms=args.expires_at_ms)
        from backend.path_identity import first_reparse_component
        output = Path(args.output)
        _require(output.is_absolute() and output.parent.is_dir() and not output.exists()
            and first_reparse_component(output) is None and not output.resolve().is_relative_to(APP.parent),
            'quality_output_path_invalid')
        verify_candidate(candidate)
        publish_json_once(output,plan)
        print(json.dumps({'output':str(output),'plan_sha256':plan['plan_sha256'],'request_authorized':False}))
        return 0
    supplied = strict_json(plan_raw)
    plan = build_batch_plan(corpus,references,candidate_sha=candidate,database_path=database,
        room_id=supplied['room_id'],not_before_ms=supplied['not_before_ms'],expires_at_ms=supplied['expires_at_ms'])
    from backend.decision_lineage import canonical_sha256
    _require(canonical_sha256(supplied) == canonical_sha256(plan), 'quality_plan_changed')
    _require(args.approve_plan_sha256 == plan['plan_sha256'], 'quality_approval_mismatch')
    _require(args.acknowledge_human_references, 'quality_human_acknowledgement_missing')
    _require(plan['not_before_ms'] <= int(time.time()*1000) < plan['expires_at_ms'], 'quality_window_expired')
    owner = DatabaseInstanceOwner(database).acquire()
    store, executor = None, None
    retain = False
    try:
        readiness = assert_database_ready_for_startup(database)
        STORE.configure_verified_startup(database,readiness['startup_identity'])
        store = STORE._resolve()
        # Refuse a spent or interrupted batch before opening a key window.
        _require(not (root/'quality-batch-receipts'/'start.json').exists(), 'quality_activation_already_claimed')
        with closing(store._connect()) as db:
            _require(db.execute('SELECT COUNT(*) FROM provider_execution_runs').fetchone()[0] == 0,
                     'quality_database_not_fresh')
            _require(db.execute('SELECT 1 FROM rooms WHERE id=?',(plan['room_id'],)).fetchone(), 'quality_room_missing')
        if args.password_dialog:
            from backend.path_identity import first_reparse_component
            _require(first_reparse_component(root/'key-input-status') is None, 'quality_key_status_path_changed')
            from scripts.pilot_password_dialog import ask_api_key
            key = ask_api_key({'config':{'pilot_id':plan['plan_sha256'],
                'provider':'doubao','model':plan['prepared']['model'],'expires_at_ms':plan['expires_at_ms'],
                'max_output_tokens':plan['prepared']['reservation']['max_output_tokens'],
                'rate_card':{'currency':'CNY'},
                'spend_plan_limit':plan['prepared']['reservation']['max_estimated_cny']},
                'max_calls':36,'plan_sha256':plan['plan_sha256']},root/'key-input-status').strip()
        else:
            _require(sys.stdin.isatty(), 'quality_masked_local_input_required')
            with warnings.catch_warnings():
                warnings.simplefilter('error',getpass.GetPassWarning)
                key = getpass.getpass('ARK_API_KEY（本机隐藏输入，不保存）: ').strip()
        from scripts.pilot_password_dialog import acceptable_password
        _require(acceptable_password(key), 'quality_invalid_credential_input')
        from backend.providers.doubao_provider import DoubaoProvider
        from backend.news_review_contracts import ENDPOINT, MODEL
        provider = DoubaoProvider(api_key=key,base_url=ENDPOINT.removesuffix('/responses'),default_model=MODEL)
        key = ''
        batch = QualityBatchLedger(store,owner,corpus_raw=corpus,reference_raw=references,
            candidate_sha=candidate,room_id=plan['room_id'],not_before_ms=plan['not_before_ms'],
            expires_at_ms=plan['expires_at_ms'],approved_plan_sha256=args.approve_plan_sha256,
            human_reference_review_acknowledged=args.acknowledge_human_references)
        from backend.news_review_quality_executor import QualityBatchExecutor
        executor = QualityBatchExecutor(batch,provider)
        result = executor.run()
        store.checkpoint_after_shutdown(instance_owner=owner)
        completed = result['execution_completed']
        publish_json_once(root/'launcher-exit.json',{'version':'news_quality_launcher_exit_v1',
            'plan_sha256':plan['plan_sha256'],'exit_code':0 if completed else 2,
            'worker_drained':True,'checkpoint_completed':True,'model_quality_verified':False})
        print(json.dumps({'ok':completed,'exit_code':0 if completed else 2,
            'plan_sha256':plan['plan_sha256'],'calls_reserved':result['batch']['calls_reserved'],
            'model_quality_verified':False,'supplier_bill_verified':False}))
        return 0 if completed else 2
    except BaseException:
        if executor and executor._worker and executor._worker.is_alive():
            executor.request_stop('quality_worker_failed')
            retain = True
            _RETAINED_OWNER = owner
        raise
    finally:
        for name in KEYS:
            os.environ.pop(name,None)
        if not retain:
            owner.release()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('{"ok":false,"code":"quality_operator_interrupted"}')
        raise SystemExit(130)
    except Exception as exc:
        import re
        value = str(exc) if type(exc) is ValueError else getattr(exc,'code','quality_run_failed')
        code = value if type(value) is str and re.fullmatch(r'[a-z_]{1,100}',value) else 'quality_run_failed'
        print(json.dumps({'ok':False,'code':code}))
        raise SystemExit(1)
