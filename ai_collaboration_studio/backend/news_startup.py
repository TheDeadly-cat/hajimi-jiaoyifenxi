"""A reversible per-user Windows login entry for the daily news workspace."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import uuid

from .path_identity import first_reparse_component


class StartupRegistrationError(RuntimeError):
    pass


def _safe(path):
    path = Path(path).absolute()
    if first_reparse_component(path) is not None:
        raise StartupRegistrationError("启动入口路径需要人工检查。")
    if path.exists() and path.is_file() and path.stat().st_nlink != 1:
        raise StartupRegistrationError("启动入口不是独立文件。")
    return path


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _startup_directory():
    if os.name != "nt":
        return None
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0007, None, 0, buffer) != 0:
        raise StartupRegistrationError("无法读取当前用户的 Windows 启动目录。")
    return _safe(buffer.value)


class NewsStartup:
    def __init__(self, app, data_directory, *, startup_directory=None, create_link=None):
        self._create_link = create_link or self._windows_link
        self._lock = threading.RLock()
        self.directory = None
        self._unavailable = ""
        self._mode = "unknown"
        if os.environ.get("AI_STUDIO_PORTABLE_NEWS") == "1":
            # The historical shortcut targets port 8770 and a parent database.
            # Do not register it for an isolated instance with different data,
            # port and proxy inheritance. Existing registrations are untouched.
            self._unavailable = "独立运行实例的登录恢复尚未绑定当前数据目录和端口，暂不支持注册；现有启动入口保持不变。"
            self._mode = "portable"
            return
        try:
            self.app = _safe(app)
            self.data = _safe(data_directory)
            self.directory = _safe(startup_directory) if startup_directory is not None else _startup_directory()
            if self.directory is not None:
                self._mode = "installed"
        except Exception:
            # Login registration is optional: a redirected/unreadable Startup
            # folder must not prevent the news service itself from starting.
            self._unavailable = "无法安全访问 Windows 登录恢复位置；消息工作台仍可正常使用。"
            return
        self.record = self.data / "news-startup-registration.json"
        self.history = self.data / "news-startup-history"
        self.pending = self.data / "news-startup-pending.json"

    @property
    def entry(self):
        return self.directory / "AIStudioDailyNews.lnk" if self.directory else None

    def _registration(self):
        _safe(self.record)
        if not self.record.exists():
            return None
        value = json.loads(self.record.read_text(encoding="utf-8"))
        if value.get("owner") != "ai_studio_daily_news_v1":
            raise StartupRegistrationError("已有启动记录无法识别。")
        return value

    def _owned(self, value):
        if not self.entry.exists():
            return False
        _safe(self.entry)
        if not value or value.get("sha256") != _sha(self.entry):
            raise StartupRegistrationError("已有启动入口被修改，未覆盖或删除。")
        return True

    def status(self):
        if self.directory is None:
            return {"mode": self._mode, "supported": False, "enabled": False,
                    "error": self._unavailable or "此功能仅支持 Windows。"}
        with self._lock:
            try:
                _safe(self.directory)
                if _safe(self.pending).exists():
                    return {"mode": self._mode, "supported": True, "enabled": False, "pending": True,
                            "error": "上次登录恢复设置未收尾；再次设置时将先核对并恢复，现有入口未改动。"}
                value = self._registration()
                enabled = self._owned(value)
                return {"mode": self._mode, "supported": True, "enabled": enabled, "error": "",
                        "version_current": bool(enabled and value.get("app") == str(self.app))}
            except Exception:
                return {"mode": self._mode, "supported": True, "enabled": False,
                        "error": "启动入口状态无法确认，请先检查；现有入口未改动。"}

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("invalid_startup_option")
        if self.directory is None:
            raise StartupRegistrationError(self._unavailable or "此功能仅支持 Windows。")
        with self._lock:
            _safe(self.directory)
            if _safe(self.pending).exists():
                self._apply_transaction(self._load_transaction())
            value = self._registration()
            owned = self._owned(value)
            if not enabled and not owned:
                return self.status()
            if enabled and owned and value.get("app") == str(self.app):
                return self.status()
            _safe(self.history).mkdir(parents=True, exist_ok=True)
            change = self.history / uuid.uuid4().hex
            change.mkdir(exist_ok=False)
            try:
                old_record = self.record.read_bytes() if self.record.exists() else None
                if old_record is not None:
                    self._write_new(change / "previous-registration.json", old_record)
                digest = None
                if enabled:
                    launcher = _safe(self.app / "start_daily_workspace.ps1")
                    if not launcher.is_file():
                        raise StartupRegistrationError("应用启动文件缺失。")
                    self._create_link(change / "new-entry.lnk", launcher)
                    digest = _sha(_safe(change / "new-entry.lnk"))
                record = {"owner": "ai_studio_daily_news_v1", "app": str(self.app),
                          "sha256": digest, "mode": "current_user_login_background",
                          "enabled": enabled, "credentials_included": False}
                after = json.dumps(record, ensure_ascii=False).encode("utf-8")
                self._write_new(change / "new-registration.json", after)
                transaction = {"owner": "ai_studio_daily_news_transaction_v1", "change_id": change.name,
                    "before_entry": _sha(self.entry) if owned else None, "after_entry": digest,
                    "before_record": hashlib.sha256(old_record).hexdigest() if old_record is not None else None,
                    "after_record": hashlib.sha256(after).hexdigest()}
                prepared = change / "prepared-transaction.json"
                self._write_new(prepared, json.dumps(transaction, ensure_ascii=False).encode("utf-8"))
                # Publish the durable intent before moving any live entry. On
                # Windows rename refuses an existing destination.
                os.rename(prepared, self.pending)
            except Exception:
                raise StartupRegistrationError("无法准备登录恢复设置；之前的入口未改动。") from None
            try:
                self._apply_transaction(transaction)
            except Exception:
                try:
                    self._rollback_transaction(transaction)
                except Exception:
                    raise StartupRegistrationError("登录恢复设置未收尾，现场已保留；请先检查现有入口。") from None
                raise StartupRegistrationError("无法保存登录恢复设置；之前的入口已恢复。") from None
            return self.status()

    @staticmethod
    def _write_new(path, content):
        with _safe(path).open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _file_digest(path):
        path = _safe(path)
        return _sha(path) if path.exists() else None

    def _load_transaction(self):
        transaction = json.loads(_safe(self.pending).read_text(encoding="utf-8"))
        if (set(transaction) != {"owner", "change_id", "before_entry", "after_entry", "before_record", "after_record"}
                or transaction["owner"] != "ai_studio_daily_news_transaction_v1"
                or not isinstance(transaction["change_id"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", transaction["change_id"])):
            raise StartupRegistrationError("未完成的登录恢复记录无法识别。")
        for name in ("before_entry", "after_entry", "before_record", "after_record"):
            digest = transaction[name]
            if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise StartupRegistrationError("未完成的登录恢复记录无法识别。")
        if transaction["after_record"] is None:
            raise StartupRegistrationError("未完成的登录恢复记录无法识别。")
        return transaction

    def _transaction_state(self, transaction):
        if self._load_transaction() != transaction:
            raise StartupRegistrationError("登录恢复记录已变化。")
        change = _safe(self.history / transaction["change_id"])
        before, after = transaction["before_entry"], transaction["after_entry"]
        entry = self._file_digest(self.entry)
        record = self._file_digest(self.record)
        backup = self._file_digest(change / "previous-entry.lnk")
        prepared = self._file_digest(change / "new-entry.lnk")
        failed = self._file_digest(change / "failed-entry.lnk")
        if (self._file_digest(change / "previous-registration.json") != transaction["before_record"]
                or self._file_digest(change / "new-registration.json") != transaction["after_record"]
                or entry not in {None, before, after}
                or record not in {transaction["before_record"], transaction["after_record"]}
                or backup not in {None, before}
                or prepared not in {None, after}
                or failed not in {None, after}
                or (before is not None and entry != before and backup != before)
                or (after is not None and entry != after and prepared != after and failed != after)):
            raise StartupRegistrationError("登录恢复文件身份不符，未覆盖或删除现有入口。")
        return change, entry, record, backup

    def _replace_record(self, content, change, expected_digest):
        if self._file_digest(self.record) != expected_digest:
            raise StartupRegistrationError("登录恢复记录已变化，未覆盖现有记录。")
        temp = change / ("record-write-" + uuid.uuid4().hex + ".json")
        self._write_new(temp, content)
        if self._file_digest(self.record) != expected_digest:
            raise StartupRegistrationError("登录恢复记录已变化，未覆盖现有记录。")
        os.replace(temp, self.record)

    def _apply_transaction(self, transaction):
        change, entry, record, backup = self._transaction_state(transaction)
        rollback = change / "rollback-started.json"
        if _safe(rollback).exists():
            if json.loads(rollback.read_text(encoding="utf-8")) != transaction:
                raise StartupRegistrationError("登录恢复回滚记录无法识别。")
            self._rollback_transaction(transaction)
            return
        before, after = transaction["before_entry"], transaction["after_entry"]
        if before is not None and entry == before and entry != after:
            if backup is not None:
                raise StartupRegistrationError("登录恢复入口出现重复副本，请先检查。")
            os.rename(self.entry, change / "previous-entry.lnk")
            entry = None
        if after is not None and entry != after:
            os.rename(change / "new-entry.lnk", self.entry)
        if record != transaction["after_record"]:
            self._replace_record((change / "new-registration.json").read_bytes(), change, record)
        os.rename(self.pending, change / "completed-transaction.json")

    def _rollback_transaction(self, transaction):
        change, entry, record, backup = self._transaction_state(transaction)
        rollback = change / "rollback-started.json"
        if not _safe(rollback).exists():
            prepared = change / ("rollback-intent-" + uuid.uuid4().hex + ".json")
            self._write_new(prepared, json.dumps(transaction, ensure_ascii=False).encode("utf-8"))
            os.rename(prepared, rollback)
        elif json.loads(rollback.read_text(encoding="utf-8")) != transaction:
            raise StartupRegistrationError("登录恢复回滚记录无法识别。")
        before, after = transaction["before_entry"], transaction["after_entry"]
        if after is not None and entry == after and entry != before:
            os.rename(self.entry, change / "failed-entry.lnk")
            entry = None
        if before is not None and entry is None:
            os.rename(change / "previous-entry.lnk", self.entry)
        if record != transaction["before_record"]:
            if transaction["before_record"] is None:
                os.rename(self.record, change / "failed-registration.json")
            else:
                self._replace_record((change / "previous-registration.json").read_bytes(), change, record)
        os.rename(self.pending, change / "rolled-back-transaction.json")

    def _windows_link(self, destination, launcher):
        script = _safe(self.app / "scripts/create_news_startup_link.ps1")
        environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
        powershell = Path(environment.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-OutputPath", str(destination), "-LauncherPath", str(launcher)],
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        if result.returncode != 0:
            raise StartupRegistrationError("创建启动入口失败。")
