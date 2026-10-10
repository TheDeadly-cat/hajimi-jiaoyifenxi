"""Launch only one sealed window outside all Windows jobs; bind external recording before key entry."""
from pathlib import Path
import datetime
import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid

ROOT = WORKSPACE = APP = TOOL = None
TOOL_SHA = "19d20dcabccabd8c37c3366311b152ff6dc3c60bce72cfba1763007ad6b01859"
RECORDER_CANDIDATE = "204f781b3599138725d605626f48a0f23ac3c232"
LAUNCH_FILES = ("launch_news_review_isolated.py", "capture_news_review_isolated.py",
                "start_news_review_isolated.ps1")
ROLES = ("launcher", "observer", "host", "independent-recorder")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def publish(name, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path = ROOT / name
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != raw:
        raise RuntimeError("isolated_receipt_readback_failed")


def utc_pin(ticks):
    seconds, fraction = divmod(int(ticks), 10_000_000)
    date = datetime.datetime(1, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return date.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction:07d}Z"


def wait_files(paths, process, seconds=30):
    end = time.monotonic() + seconds
    while not all(path.is_file() for path in paths):
        if process.poll() is not None:
            raise RuntimeError("prepared_process_ended_before_ready")
        if time.monotonic() >= end:
            raise RuntimeError("prepared_process_readiness_unconfirmed")
        time.sleep(0.03)


class BoundaryFault(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def load_native():
    if hashlib.sha256(TOOL.read_bytes()).hexdigest() != TOOL_SHA:
        raise BoundaryFault("recorder_source_changed")
    spec = importlib.util.spec_from_file_location("isolated_exit_native", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    native = module.WindowsProcesses()
    native.dll.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    native.dll.IsProcessInJob.restype = ctypes.c_int
    native.dll.ProcessIdToSessionId.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    native.dll.ProcessIdToSessionId.restype = ctypes.c_int
    return module, native


def process_boundary(native, pin, *, expected_session=None):
    handle = native.open(pin["pid"])
    try:
        if str(native.times(handle)[0]) != str(pin["process_start_utc_ticks"]):
            raise BoundaryFault("boundary_identity_mismatch")
        if native.dll.WaitForSingleObject(handle, 0) != 258:
            raise BoundaryFault("boundary_process_not_live")
        in_job = ctypes.c_int()
        if not native.dll.IsProcessInJob(handle, None, ctypes.byref(in_job)):
            raise BoundaryFault("boundary_job_query_unconfirmed")
        if in_job.value:
            raise BoundaryFault("boundary_process_still_in_job")
        session = ctypes.c_uint32()
        if not native.dll.ProcessIdToSessionId(pin["pid"], ctypes.byref(session)):
            raise BoundaryFault("boundary_session_unconfirmed")
        if expected_session is not None and session.value != expected_session:
            raise BoundaryFault("boundary_session_mismatch")
        return {"pin": pin, "outside_all_jobs": True, "session_id": int(session.value),
                "checked_at_ms": time.time_ns() // 1000000,
                "basis": "native_live_handle_and_creation_ticks"}
    finally:
        native.dll.CloseHandle(handle)


def interactive_desktop_boundary(native):
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetProcessWindowStation.argtypes = []
    user.GetProcessWindowStation.restype = ctypes.c_void_p
    user.GetThreadDesktop.argtypes = [ctypes.c_uint32]
    user.GetThreadDesktop.restype = ctypes.c_void_p
    user.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                              ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    user.GetUserObjectInformationW.restype = ctypes.c_int
    native.dll.GetCurrentThreadId.argtypes = []
    native.dll.GetCurrentThreadId.restype = ctypes.c_uint32

    def object_name(handle):
        if not handle:
            raise BoundaryFault("desktop_handle_unconfirmed")
        needed = ctypes.c_uint32()
        user.GetUserObjectInformationW(handle, 2, None, 0, ctypes.byref(needed))
        if not 2 <= needed.value <= 1024 or needed.value % ctypes.sizeof(ctypes.c_wchar):
            raise BoundaryFault("desktop_name_length_unconfirmed")
        buffer = ctypes.create_unicode_buffer(needed.value // ctypes.sizeof(ctypes.c_wchar))
        if not user.GetUserObjectInformationW(handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            raise BoundaryFault("desktop_name_unconfirmed")
        return buffer.value

    # Both handles are borrowed from this process/thread and must not be closed.
    station = object_name(user.GetProcessWindowStation())
    desktop = object_name(user.GetThreadDesktop(native.dll.GetCurrentThreadId()))
    if station.casefold() != "winsta0" or desktop.casefold() != "default":
        raise BoundaryFault("interactive_desktop_mismatch")
    return {"window_station": station, "desktop": desktop,
            "basis": "native_names_for_current_process_and_thread",
            "credential_window_display_verified": False}


def boundary_receipt_name(role):
    if role not in ROLES:
        raise BoundaryFault("boundary_role_invalid")
    # The observer reserves host-*.json for exactly one native host receipt.
    return "job-boundary-" + role + ".json"


def configure_root(root):
    global ROOT, WORKSPACE, APP, TOOL
    ROOT = Path(root)
    if not ROOT.is_absolute():
        raise BoundaryFault("absolute_run_root_required")
    bindings = read(ROOT / "isolated-launch-bindings.json")
    if (set(bindings) != {"version", "candidate_workspace", "recorder_script"}
            or bindings["version"] != "news_review_isolated_launch_bindings_v1"):
        raise BoundaryFault("launcher_bindings_invalid")
    WORKSPACE = Path(bindings["candidate_workspace"])
    TOOL = Path(bindings["recorder_script"])
    if not WORKSPACE.is_absolute() or not TOOL.is_absolute():
        raise BoundaryFault("absolute_source_paths_required")
    APP = WORKSPACE / "ai_collaboration_studio"
    if ROOT.resolve().is_relative_to(WORKSPACE.resolve()):
        raise BoundaryFault("isolated_trial_required")


def preflight(own_boundary):
    scope = read(ROOT / "window-execution-scope.json")
    contact = read(ROOT / "window-sec-contact-authorization.json")["user_agent"]
    policy = read(ROOT / "window-policy-template.json")
    observer = read(ROOT / "observer-plan.json")
    approval = read(ROOT / "user-approval.json")
    recorder_ci = read(ROOT / "recorder-ci-verification.json")
    manifest = read(ROOT / "preparation-manifest.json")
    runtime_ci = read(ROOT / "ci-verification.json")
    duration_ms = scope["duration_ms"]
    modes = {900000: (2, "0.50"), 86400000: (8, "2.00")}
    if type(duration_ms) is not int or duration_ms not in modes:
        raise BoundaryFault("unapproved_window_mode")
    model_cap, spend_cap = modes[duration_ms]
    if approval.get("expected_session_id") != own_boundary["session_id"]:
        raise BoundaryFault("approved_session_changed")
    if (approval.get("preparation_manifest_sha256") != hashlib.sha256(
            (ROOT / "preparation-manifest.json").read_bytes()).hexdigest()
            or approval.get("candidate_sha") != scope["candidate_sha"]
            or manifest.get("candidate_sha") != scope["candidate_sha"]
            or manifest.get("activation_sha256") != scope["activation_sha256"]):
        raise RuntimeError("prepared_scope_identity_changed")
    for entry in manifest["files"]:
        if (Path(entry["name"]).name != entry["name"] or hashlib.sha256(
                (ROOT / entry["name"]).read_bytes()).hexdigest() != entry["sha256"]):
            raise RuntimeError("prepared_file_changed")
    sealed_names = [entry["name"] for entry in manifest["files"]]
    required_names = {*LAUNCH_FILES, "isolated-launch-bindings.json", "launcher-ci-verification.json",
                      "window-execution-scope.json", "window-sec-contact-authorization.json",
                      "window-policy-template.json", "activation-proposal.json", "ci-verification.json",
                      "recorder-ci-verification.json"}
    if len(set(sealed_names)) != len(sealed_names) or not required_names <= set(sealed_names):
        raise BoundaryFault("launcher_materials_not_sealed")
    if not (approval["approved"] is True and approval["activation_sha256"] == scope["activation_sha256"]
            and scope["duration_ms"] == duration_ms and scope["max_model_calls"] == model_cap
            and scope["spend_limit_cny"] == spend_cap
             and policy["candidate_sha"] == scope["candidate_sha"]
             and policy["max_model_calls"] == model_cap and policy["spend_limit_cny"] == spend_cap
             and policy["sources"] == ["sec_filings:US.NVDA:8-K", "company_ir:US.MU:recent-30"]
             and policy["provider"] == "doubao"
             and policy["model"] == "doubao-seed-2-1-pro-260915"
             and policy["endpoint"] == "https://ark.cn-beijing.volces.com/api/v3/responses"
             and policy["max_document_requests"] == 16 and policy["max_request_bytes"] == 32768
             and policy["max_output_tokens"] == 1400 and policy["concurrency"] == 1
             and policy["stop_on_unknown"] is True
             and policy["resume_within_window"] is False and "@" in contact
            and recorder_ci.get("evidence_reviewed") is True
            and recorder_ci.get("tool_source_sha256") == TOOL_SHA
            and len(recorder_ci["workflow_runs"]) == 2
            and len({r["id"] for r in recorder_ci["workflow_runs"]}) == 2
            and all(r["head_sha"] == RECORDER_CANDIDATE
                    and r["status"] == "completed" and r["conclusion"] == "success"
                    for r in recorder_ci["workflow_runs"])):
        raise RuntimeError("isolated_scope_unconfirmed")
    required_steps = ['Deterministic offline security baseline', 'Bootstrap locked dependencies and production frontend', 'Guarded frontend regression', 'Required historical reader compatibility matrix', 'Full isolated backend regression', 'Clean-source install, test, build, and startup smoke', 'Isolated install, upgrade, and rollback drill', 'Generate offline dependency inventory', 'Upload non-secret delivery receipts']
    if (runtime_ci.get("evidence_reviewed") is not True
            or runtime_ci.get("candidate_sha") != scope["candidate_sha"]
            or len(runtime_ci.get("workflow_runs", [])) != 2
            or len({r["id"] for r in runtime_ci["workflow_runs"]}) != 2):
        raise BoundaryFault("runtime_ci_unconfirmed")
    for run in runtime_ci["workflow_runs"]:
        steps = {step["name"]: step for step in run.get("required_steps", [])}
        if (run["head_sha"] != scope["candidate_sha"] or run["status"] != "completed"
                or run["conclusion"] != "success" or set(steps) != set(required_steps)
                or any(step["status"] != "completed" or step["conclusion"] != "success" for step in steps.values())):
            raise BoundaryFault("runtime_ci_steps_incomplete")
    if not policy["not_before_ms"] <= time.time_ns() // 1000000 < policy["expires_at_ms"]:
        raise RuntimeError("isolated_activation_eligibility_expired")
    if hashlib.sha256(TOOL.read_bytes()).hexdigest() != TOOL_SHA:
        raise RuntimeError("recorder_source_changed")
    for path in (ROOT / "activation-policy.json", ROOT / "activation-receipt.json", ROOT / "monitoring",
                 ROOT / "independent-exit", ROOT / "window-report.json"):
        if path.exists():
            raise RuntimeError("isolated_run_already_claimed")
    for run in recorder_ci["workflow_runs"]:
        steps = {step["name"]: step for step in run.get("required_steps", [])}
        if (set(steps) != set(required_steps)
                or any(step["status"] != "completed" or step["conclusion"] != "success" for step in steps.values())):
            raise BoundaryFault("recorder_ci_steps_incomplete")
    launcher_ci = read(ROOT / "launcher-ci-verification.json")
    if (launcher_ci.get("evidence_reviewed") is not True
            or type(launcher_ci.get("candidate_sha")) is not str
            or len(launcher_ci["candidate_sha"]) != 40
            or len(launcher_ci.get("workflow_runs", [])) != 2
            or len({run["id"] for run in launcher_ci["workflow_runs"]}) != 2
            or set(launcher_ci.get("source_files", {})) != set(LAUNCH_FILES)):
        raise BoundaryFault("launcher_ci_unconfirmed")
    for name, expected_sha in launcher_ci["source_files"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected_sha:
            raise BoundaryFault("launcher_source_changed")
    for run in launcher_ci["workflow_runs"]:
        steps = {step["name"]: step for step in run.get("required_steps", [])}
        if (run["head_sha"] != launcher_ci["candidate_sha"] or run["status"] != "completed"
                or run["conclusion"] != "success" or set(steps) != set(required_steps)
                or any(step["status"] != "completed" or step["conclusion"] != "success" for step in steps.values())):
            raise BoundaryFault("launcher_ci_steps_incomplete")
    # Disallow a legacy boundary name or a pre-existing native host before key input.
    if list(ROOT.glob("host-*.json")):
        raise BoundaryFault("unclaimed_host_namespace_required")
    return scope, contact, policy, observer, duration_ms


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one sealed window through a verified isolated Windows launcher")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--launch", action="store_true")
    mode.add_argument("--check-boundary-only", action="store_true")
    parser.add_argument("--run-root", required=True)
    args = parser.parse_args(argv)
    configure_root(args.run_root)
    native_module, native = load_native()
    native_module.no_reparse(ROOT)
    native_module.no_reparse(WORKSPACE)
    native_module.no_reparse(TOOL)
    own_boundary = process_boundary(native, native.pin(os.getpid()))
    own_boundary["interactive_desktop"] = interactive_desktop_boundary(native)
    if args.check_boundary_only:
        own_boundary.update(real_source_requests_started=False, model_requests_started=False,
                            database_opened=False, trial_started=False)
        publish("boundary-only-" + str(os.getpid()) + ".json", own_boundary)
        print(json.dumps(own_boundary), flush=True)
        # This diagnostic mode stays live briefly so its caller can pin a handle.
        # It never reads execution scope or initializes the product.
        time.sleep(5)
        return 0
    scope, contact, policy, observer, duration_ms = preflight(own_boundary)
    env = os.environ.copy()
    for name in ("OPENAI_API_KEY", "ARK_API_KEY", "DOUBAO_API_KEY", "DEEPSEEK_API_KEY", "QWEN_API_KEY",
                 "DASHSCOPE_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY", "ZHIPUAI_API_KEY", "BAILIAN_TOKEN_PLAN_API_KEY"):
        env.pop(name, None)
    env.update(AI_STUDIO_SKIP_LOCAL_ENV="1", AI_STUDIO_RUNTIME_DIR=str(ROOT / "runtime"),
               AI_STUDIO_DATABASE_PATH=policy["database_path"], GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0=str(WORKSPACE))
    for args, expected in ((["rev-parse", "HEAD"], scope["candidate_sha"]), (["status", "--porcelain"], "")):
        value = subprocess.check_output(["git", "-C", str(WORKSPACE), *args], env=env).decode("ascii").strip()
        if value != expected:
            raise RuntimeError("isolated_candidate_changed")
    publish(boundary_receipt_name("launcher"), own_boundary)
    streams, processes = [], {}
    gate_released = False
    awake_hold = False
    power = None
    result = {"version": "isolated_external_launch_result_v1", "candidate_sha": scope["candidate_sha"],
              "activation_sha256": scope["activation_sha256"], "completed": False,
              "model_quality_verified": False, "supplier_bill_verified": False, "release_approved": False, "launcher_outside_all_jobs": True}

    def spawn(role, command, *, stdin=subprocess.DEVNULL):
        output = (ROOT / (role + ".stdout.txt")).open("xb", buffering=0)
        errors = (ROOT / (role + ".stderr.txt")).open("xb", buffering=0)
        streams.extend((output, errors))
        process = subprocess.Popen(command, cwd=APP, env=env, stdin=stdin, stdout=output, stderr=errors,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        processes[role] = process
        boundary = process_boundary(native, native.pin(process.pid), expected_session=own_boundary["session_id"])
        publish(boundary_receipt_name(role), boundary)
        return process

    try:
        import ctypes
        power = ctypes.WinDLL("kernel32", use_last_error=True)
        power.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        power.SetThreadExecutionState.restype = ctypes.c_uint32
        awake_hold = bool(power.SetThreadExecutionState(0x80000001))
        if not awake_hold:
            raise RuntimeError("isolated_process_awake_hold_unconfirmed")
        result["automatic_sleep_hold"] = "system_required_for_this_process_lifetime"
        result["persistent_power_settings_changed"] = False
        watching = spawn("observer", [sys.executable, "-X", "utf8", str(APP / "scripts/run_news_review_observer.py"),
            "--run", "--plan", str(ROOT / "observer-plan.json"),
            "--approve-plan-sha256", observer["plan_sha256"]])
        monitor_root = ROOT / "monitoring"
        wait_files([monitor_root / "observer-pin.json", monitor_root / "monitor-wait-execution-000001.json"], watching)
        observer_pin = read(monitor_root / "observer-pin.json")
        if native.pin(watching.pid)["process_start_utc_ticks"] != str(observer_pin["process_start_utc_ticks"]):
            raise RuntimeError("observer_generation_unconfirmed")
        # A non-secret gate byte is the only stdin input. Product code and its
        # masked credential window start only after both process handles bind.
        bootstrap = ("import sys,runpy; p=sys.argv[1];sys.argv=sys.argv[1:];"
                     "b=sys.stdin.buffer.read(1);"
                     "assert b==b'G';runpy.run_path(p,run_name='__main__')")
        host = spawn("host", [sys.executable, "-X", "utf8", "-c", bootstrap,
            str(APP / "scripts/run_news_review_trial.py"), "--activate", "--config", str(ROOT / "window-policy-template.json"),
            "--approve-activation-sha256", scope["activation_sha256"], "--duration-ms", str(duration_ms),
            "--password-dialog", "--sec-user-agent", contact, "--port", "0",
            "--output", str(ROOT / "window-report.json")], stdin=subprocess.PIPE)
        host_pin = native.pin(host.pid)
        stamp = utc_pin(host_pin["process_start_utc_ticks"])
        publish("launch-receipt.json", {"version": "isolated_native_launch_receipt_v1",
            "candidate_sha": scope["candidate_sha"], "activation_sha256": scope["activation_sha256"],
            "process_id": host.pid, "process_start_utc": stamp,
            "output_path": str(ROOT / "window-report.json"), "gate_released": False})
        publish("launcher-start.json", {"candidate_sha": scope["candidate_sha"],
            "activation_sha256": scope["activation_sha256"], "launcher_pid": host.pid,
            "launcher_process_start_utc": stamp, "bootstrap_holding": True})
        maximum_end = policy["expires_at_ms"] + duration_ms + 255000 + 120000
        request = {"version": "news_review_exit_request_v1", "run_id": uuid.uuid4().hex,
            "identity": {"candidate_sha": scope["candidate_sha"], "policy_sha256": None,
                         "activation_sha256": scope["activation_sha256"], "observer_plan_sha256": observer["plan_sha256"]},
            "maximum_observation_ms": max(1, maximum_end - time.time_ns() // 1000000),
            "targets": [{**host_pin, "role": "host"}, {"pid": watching.pid, "role": "observer",
                "process_start_utc_ticks": str(observer_pin["process_start_utc_ticks"])}]}
        publish("independent-exit-request.json", native_module.validate_request(request))
        digest = hashlib.sha256((ROOT / "independent-exit-request.json").read_bytes()).hexdigest()
        recording = spawn("independent-recorder", [sys.executable, "-X", "utf8", "-I", "-S", str(TOOL),
            "--request", str(ROOT / "independent-exit-request.json"), "--request-sha256", digest,
            "--output", str(ROOT / "independent-exit")])
        wait_files([ROOT / "independent-exit/target-host-bound.json", ROOT / "independent-exit/target-observer-bound.json"], recording)
        publish("external-recording-ready.json", {"candidate_sha": scope["candidate_sha"],
            "activation_sha256": scope["activation_sha256"], "request_sha256": digest,
            "observer_plan_sha256": observer["plan_sha256"], "tool_source_sha256": TOOL_SHA,
            "host_pin": host_pin, "observer_pin": observer_pin,
            "ready_at_ms": time.time_ns() // 1000000, "model_key_window_opened_by_wrapper": False,
            "policy_binding_basis": "unresolved_before_activation; associate later through activation receipt and identical host pin"})
        host.stdin.write(b"G")
        host.stdin.flush()
        host.stdin.close()
        gate_released = True
        print(json.dumps({"phase": "external_recording_armed_key_window_released",
                          "host_pid": host.pid, "observer_pid": watching.pid,
                          "activation_sha256": scope["activation_sha256"]}), flush=True)
        for role, process in processes.items():
            seconds = max(1, (maximum_end - time.time_ns() // 1000000) / 1000)
            result[role + "_exit_code"] = process.wait(timeout=seconds)
        result["completed"] = all(result[role + "_exit_code"] == 0 for role in processes)
        result["window_report_present"] = (ROOT / "window-report.json").is_file()
        result["completed"] = result["completed"] and result["window_report_present"]
    except Exception as exc:
        result["failure_type"] = type(exc).__name__
    finally:
        # Never kill a live trial or paid worker. An unreleased bootstrap has no
        # product code running; closing its gate makes its assertion exit safely.
        if not gate_released and "host" in processes and processes["host"].stdin:
            processes["host"].stdin.close()
        result["process_exit_codes"] = {role: process.poll() for role, process in processes.items()}
        result["live_processes_unconfirmed"] = [role for role, code in result["process_exit_codes"].items() if code is None]
        if result["live_processes_unconfirmed"]:
            result["completed"] = False
        for stream in streams:
            try:
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
            except OSError:
                result["completed"] = False
                result["log_storage_unconfirmed"] = True
        if awake_hold and power is not None:
            result["awake_hold_released"] = bool(power.SetThreadExecutionState(0x80000000))
            if not result["awake_hold_released"]:
                result["completed"] = False
        result["ended_at_ms"] = time.time_ns() // 1000000
        publish("external-launch-result.json", result)
    print(json.dumps(result), flush=True)
    return 0 if result["completed"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"phase": "isolated_launcher_failed", "failure_type": type(exc).__name__,
                          "failure_code": exc.code if isinstance(exc, BoundaryFault) else "unclassified_failure",
                          "completed": False}), flush=True)
        raise SystemExit(2)
