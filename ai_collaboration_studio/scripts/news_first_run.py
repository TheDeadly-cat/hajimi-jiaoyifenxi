"""Small coordinator for the Windows welcome screen; no parallel installer.

The UI obtains consent. Dependency installation and data admission stay in the
existing bootstrap and portable entry. Only a process created here can be
recognized as this workspace on a later open.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from scripts import news_portable_runtime as portable
from scripts import bootstrap_ai_collaboration_studio as bootstrap


class SetupError(RuntimeError):
    pass


MESSAGES = {
    "dependencies_missing": ("还需准备运行组件", "确认后将按固定版本安装组件并准备工作台；不会创建或修改已有消息。", "install"),
    "first_run": ("欢迎使用消息工作台", "将为你创建独立的消息保存位置。免费阅读不需要 AI 密钥；采集和 AI 均由你选择开启。", "initialize"),
    "ready_to_start": ("已有消息已找到", "继续打开原来的消息保存位置，不重复初始化。", "launch"),
    "already_running": ("工作台正在运行", "已确认是这个工作台及原消息保存位置，可直接打开。", "open"),
    "recovery_required": ("已有数据需要安全检查", "已有文件保持原状，未删除、恢复或迁移。请保存诊断并保留此数据目录；不要删除数据库附属文件。", "diagnose"),
    "partial_data": ("已有数据不完整，暂不能继续", "不会用新空数据覆盖已有文件。请保存诊断，保留原目录及备份后进行安全处理。", "diagnose"),
    "occupied_port": ("工作台地址正在被其他程序使用", "未关闭或替换任何程序，也未打开身份不明的页面。请保存诊断后处理地址冲突。", "diagnose"),
    "identity_unknown": ("无法确认先前运行实例", "暂不打开或再启动，避免误用另一份消息。已有进程和数据保持原状。", "diagnose"),
    "startup_failed": ("工作台未能打开", "已有数据保持原状。请复制安全诊断；不要反复启动或手工删除数据库文件。", "diagnose"),
    "startup_pending": ("打开结果尚未确认", "另一个打开操作或已绑定后台仍在准备。不会再启动一份；请稍后重新检查，现有进程保持。", "diagnose"),
    "preparation_pending": ("另一窗口仍在准备组件", "未重复安装。请回到原来的欢迎窗口继续核对，原数据和运行进程保持。", "diagnose"),
    "dependency_install_failed": ("运行组件未准备完成", "已保留安装回执。请检查网络或剩余空间后重新检查；不会自动重试下载。", "diagnose"),
    "download_confirmation_required": ("安装尚未获准", "只有确认安装后才会下载固定依赖。", "install"),
    "offline_install_refused": ("离线验证不会下载组件", "请使用已经准备好的独立运行环境，或退出离线验证后明确确认安装。", "diagnose"),
    "invalid_location": ("此保存位置暂不能使用", "请使用独立的普通文件夹；不能使用链接、已有文件别名或受保护地址。", "diagnose"),
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


def status(code, **extra):
    title, detail, action = MESSAGES[code]
    return {"code": code, "title": title, "detail": detail, "action": action,
            "mode": "portable", "login_startup_supported": False, **extra}


def launch_key(project, data, port):
    text = json.dumps([str(project).casefold(), str(data).casefold(), port], ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def binding_path(project, data, port):
    return project / "runtime/news-launcher/instances" / (launch_key(project, data, port) + ".json")


@contextmanager
def launch_gate(project, data, port, *, timeout_ms=25000, timeout_code="startup_pending"):
    """Serialize the existing launch transaction across welcome processes."""
    if os.name != "nt":
        raise SetupError("identity_unknown")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateMutexW(None, False, "Local\\NewsWorkspaceLaunch_" + launch_key(project, data, port))
    if not handle:
        raise SetupError("identity_unknown")
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, timeout_ms)
        if result == 258:
            raise SetupError(timeout_code)
        if result not in (0, 0x80):
            raise SetupError("identity_unknown")
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def write_binding(path, value):
    path = portable.unaliased_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".new")
    portable.write_receipt(temporary, value)
    portable.unaliased_path(path)
    os.replace(temporary, path)


def process_identity(pid):
    """Full Windows creation FILETIME, never PID-only identity."""
    if os.name != "nt":
        raise SetupError("identity_unknown")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise SetupError("identity_unknown")
    try:
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise SetupError("identity_unknown")
        return {"pid": int(pid), "creation_filetime": str((created.dwHighDateTime << 32) | created.dwLowDateTime),
                "exited": bool(exited.dwHighDateTime or exited.dwLowDateTime)}
    finally:
        kernel.CloseHandle(handle)


def listener_pid(port):
    if os.name != "nt":
        raise SetupError("identity_unknown")
    class Row(ctypes.Structure):
        _fields_ = [(n, wintypes.DWORD) for n in ("state", "address", "port", "remote", "remote_port", "pid")]
    api = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    api.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG), wintypes.BOOL, wintypes.ULONG, wintypes.ULONG, wintypes.ULONG)
    api.restype = wintypes.DWORD
    for _ in range(3):
        size = wintypes.ULONG()
        result = api(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if result not in (0, 122) or size.value < 4:
            raise SetupError("identity_unknown")
        buffer = ctypes.create_string_buffer(size.value)
        result = api(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if result == 122:
            continue
        if result:
            raise SetupError("identity_unknown")
        count = wintypes.DWORD.from_buffer(buffer).value
        if 4 + count * ctypes.sizeof(Row) > len(buffer):
            raise SetupError("identity_unknown")
        owners = {Row.from_buffer(buffer, 4 + i * ctypes.sizeof(Row)).pid for i in range(count)
                  if socket.ntohs(Row.from_buffer(buffer, 4 + i * ctypes.sizeof(Row)).port & 0xFFFF) == port}
        if len(owners) > 1:
            raise SetupError("identity_unknown")
        return next(iter(owners), None)
    raise SetupError("identity_unknown")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SetupError("identity_unknown")


def get_local(port, endpoint):
    if endpoint not in ("/api/readiness", "/api/version"):
        raise SetupError("identity_unknown")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(f"http://127.0.0.1:{port}{endpoint}", timeout=2) as response:
        raw = response.read(262145)
    if len(raw) > 262144:
        raise SetupError("identity_unknown")
    return json.loads(raw.decode("utf-8"))


def own_instance(project, data, port, *, identity=process_identity, listener=listener_pid, get=get_local):
    stored = binding_path(project, data, port)
    owner = listener(port)
    if not stored.exists():
        return "occupied_port" if owner is not None else None
    try:
        record = json.loads(portable.unaliased_path(stored).read_text(encoding="utf-8"))
        expected = {"project": str(project), "data_root": str(data), "port": port}
        if any(record.get(k) != v for k, v in expected.items()):
            return "identity_unknown"
        actual = identity(record["pid"])
        if actual is None or actual["exited"]:
            return "occupied_port" if owner is not None else None
        if actual["creation_filetime"] != record.get("creation_filetime"):
            return "identity_unknown"
        if owner is None:
            return "startup_pending"
        if owner != record["pid"]:
            return "occupied_port"
        ready, version = get(port, "/api/readiness"), get(port, "/api/version")
        if (ready.get("ready") is True and ready.get("service", {}).get("id") == "ai_collaboration_studio"
                and version.get("service", {}).get("id") == "ai_collaboration_studio"
                and version.get("backend_build", {}).get("source_sha256") == record["backend_sha256"] == portable.backend_identity(project)
                and version.get("frontend_build", {}).get("index_sha256") == record["frontend_sha256"] == sha(project / "frontend/dist/index.html")):
            return "already_running"
        return "identity_unknown"
    except (OSError, ValueError, KeyError, TypeError, SetupError):
        return "identity_unknown"


def dependency_ready(project, python):
    if not python.is_file() or not (project / "frontend/dist/index.html").is_file():
        return False
    expected = bootstrap.parse_hashed_lock(project / "requirements-lock-win-py314.txt")
    code = "import importlib.metadata,json,sys;print(json.dumps({'python':list(sys.version_info[:2]),'packages':{n:importlib.metadata.version(n) for n in json.loads(sys.argv[1])}}))"
    try:
        result = subprocess.run([str(python), "-I", "-B", "-X", "utf8", "-c", code, json.dumps(list(expected))],
                                cwd=project, env=portable.safe_environment(), capture_output=True, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        observed = json.loads(result.stdout.decode("utf-8"))
        return result.returncode == 0 and observed["python"] == [3, 14] and observed["packages"] == expected
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False


def inspect_workspace(project, data, port, python, *, own=own_instance, dependencies=dependency_ready):
    running = own(project, data, port)
    if running:
        return status(running)
    if not dependencies(project, python):
        return status("dependencies_missing")
    if not data.exists() or (data.is_dir() and not any(data.iterdir())):
        return status("first_run")
    if not data.is_dir():
        return status("invalid_location")
    for name in portable.DATABASE_NAMES:
        p = data / name
        if any(Path(str(p) + suffix).exists() for suffix in ("-wal", "-shm", "-journal", ".startup-recovery.pending.json")):
            return status("recovery_required")
    if not all((data / name).is_file() and (data / name).stat().st_size for name in portable.DATABASE_NAMES):
        return status("partial_data")
    return status("ready_to_start")


def launch(project, data, port, python, *, initialize=False, offline=False, receipt_root=None,
           popen=None, controlled_stdin=False):
    with launch_gate(project, data, port):
        return _launch_owned(project, data, port, python, initialize=initialize, offline=offline,
                             receipt_root=receipt_root, popen=popen, controlled_stdin=controlled_stdin)


def _launch_owned(project, data, port, python, *, initialize=False, offline=False, receipt_root=None,
                  popen=None, controlled_stdin=False):
    before = inspect_workspace(project, data, port, python)
    if before["code"] == "already_running":
        return before
    expected = "first_run" if initialize else "ready_to_start"
    if before["code"] != expected:
        return before
    receipt_root = receipt_root or project / "runtime/news-launcher/receipts"
    child_receipt = receipt_root / (uuid.uuid4().hex + "-child.json")
    command = [str(python), "-I", "-B", "-X", "utf8", str(project / "scripts/run_news_workspace.py"),
               "--data-root", str(data), "--port", str(port), "--receipt", str(child_receipt)]
    if initialize:
        command.append("--initialize-new")
    if offline:
        command.append("--offline")
    if controlled_stdin:
        command.append("--controlled-stdin")
    # The portable entry validates the effective proxy view before sanitizing
    # it; do not erase the user's validated online source proxy in this parent.
    environment = dict(os.environ)
    for name in portable.PROVIDER_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    child = (popen or subprocess.Popen)(command, cwd=project, env=environment,
                stdin=subprocess.PIPE if controlled_stdin else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    actual = process_identity(child.pid)
    if actual is None or actual["exited"]:
        return status("startup_failed")
    record = {"project": str(project), "data_root": str(data), "port": port, "pid": child.pid,
              "creation_filetime": actual["creation_filetime"], "created_at": utc(), "offline": offline,
              "backend_sha256": portable.backend_identity(project), "frontend_sha256": sha(project / "frontend/dist/index.html")}
    write_binding(binding_path(project, data, port), record)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if child.poll() is not None:
            error = "startup_failed"
            if child_receipt.exists():
                try:
                    value = json.loads(child_receipt.read_text(encoding="utf-8"))
                    if value.get("error_code") in ("database_sidecars_require_explicit_recovery", "news_schema_requires_explicit_migration", "news_indexes_require_explicit_migration") or value.get("error_type") in ("DatabaseMigrationRequired", "DatabaseMigrationRecoveryRequired", "StartupRecoveryError"):
                        error = "recovery_required"
                except (OSError, ValueError):
                    pass
            return status(error, child_exit_code=child.returncode, child_receipt=str(child_receipt))
        if own_instance(project, data, port) == "already_running":
            return status("already_running", created_this_time=True, child_receipt=str(child_receipt))
        time.sleep(0.2)
    return status("startup_pending", child_receipt=str(child_receipt))


def safe_diagnostic(value):
    return "消息工作台安全诊断\n状态：%s\n运行方式：独立消息工作台\n未包含密钥、消息正文或本机完整路径。" % value["code"]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("inspect", "bootstrap", "launch"))
    parser.add_argument("--project-root", default=str(PROJECT))
    parser.add_argument("--data-root")
    parser.add_argument("--port", type=int, default=portable.DEFAULT_PORT)
    parser.add_argument("--python")
    parser.add_argument("--initialize-new", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--allow-dependency-downloads", action="store_true")
    args = parser.parse_args(argv)
    attempt = uuid.uuid4().hex
    receipt_path = PROJECT / "runtime/news-launcher/receipts" / (attempt + ".json")
    started = utc()
    try:
        project = portable.unaliased_path(args.project_root)
        if project != PROJECT.resolve():
            raise SetupError("invalid_location")
        data = portable.data_root(project, args.data_root)
        portable.validate_port(args.port)
        python = portable.unaliased_path(args.python or bootstrap.managed_python_path(project / "runtime/bootstrap"))
        if args.action == "bootstrap":
            if args.offline:
                raise SetupError("offline_install_refused")
            if not args.allow_dependency_downloads:
                raise SetupError("download_confirmation_required")
            # This project-wide guard also survives an unexpectedly closed UI:
            # a still-running bootstrap child keeps its own preparation lock.
            with launch_gate(project, project, 0, timeout_code="preparation_pending"):
                if not dependency_ready(project, bootstrap.managed_python_path(project / "runtime/bootstrap")):
                    bootstrap.bootstrap(project_root=project, runtime_root=project / "runtime/bootstrap",
                                        allow_dependency_downloads=True, check_only=False, report_path=None)
            value = inspect_workspace(project, data, args.port, bootstrap.managed_python_path(project / "runtime/bootstrap"))
        elif args.action == "launch":
            value = launch(project, data, args.port, python, initialize=args.initialize_new, offline=args.offline)
        else:
            value = inspect_workspace(project, data, args.port, python)
    except SetupError as exc:
        value = status(str(exc) if str(exc) in MESSAGES else "startup_failed")
    except portable.PortableError:
        value = status("invalid_location")
    except bootstrap.BootstrapError:
        value = status("dependency_install_failed")
    except Exception:
        value = status("startup_failed")
    value.update(schema="news_first_run_v1", started_at=started, finished_at=utc(),
                 paid_model_calls=0, production_processes_stopped=0, automatic_recovery=False,
                 diagnostic=safe_diagnostic(value), receipt=str(receipt_path))
    try:
        portable.write_receipt(receipt_path, value)
    except Exception:
        value["receipt"] = None
        value["diagnostic"] += "\n本次诊断文件未能保存，请复制以上内容。"
    print(json.dumps(value, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
