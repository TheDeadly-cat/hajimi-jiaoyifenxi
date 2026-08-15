from __future__ import annotations

import json
import os
import socket
import stat
import time
from pathlib import Path
from typing import BinaryIO, Any

from backend.path_identity import first_reparse_component


class InstanceAlreadyRunning(RuntimeError):
    """Raised when another process already owns the same studio database."""


class DatabaseInstanceOwner:
    """Cross-process, crash-releasing ownership for one SQLite database.

    The lock file is intentionally retained. File existence is not ownership;
    only the operating-system lock on its first byte is authoritative.
    """

    def __init__(self, database_path: str | Path) -> None:
        self._requested_database_path = Path(database_path).expanduser()
        self._assert_database_path_identity(self._requested_database_path)
        self.database_path = self._requested_database_path.resolve()
        self.lock_path = self.database_path.with_name(
            f"{self.database_path.name}.owner.lock"
        )
        self._handle: BinaryIO | None = None
        self._held = False

    def _assert_database_path_identity(self, requested: Path | None = None) -> None:
        """Reject an existing database alias before resolving its path."""

        requested = requested or self._requested_database_path
        offending_component = first_reparse_component(requested)
        if offending_component is not None:
            raise RuntimeError(
                "Database path may not contain a symlink or reparse point: "
                f"{offending_component}"
            )
        if not os.path.lexists(os.fspath(requested)):
            return
        try:
            metadata = requested.lstat()
        except OSError as exc:
            raise RuntimeError(
                f"Cannot inspect database path identity: {requested}"
            ) from exc
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
        if requested.is_symlink() or bool(
            reparse_flag
            and int(getattr(metadata, "st_file_attributes", 0) or 0) & reparse_flag
        ):
            raise RuntimeError(
                f"Database path may not be a symlink or reparse point: {requested}"
            )
        if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
            raise RuntimeError(
                f"Database path must be an independent regular file: {requested}"
            )

    @property
    def held(self) -> bool:
        return self._held and self._handle is not None and not self._handle.closed

    def acquire(self, *, metadata: dict[str, Any] | None = None) -> "DatabaseInstanceOwner":
        if self.held:
            return self
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._open_unaliased_lock_file()
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() < 1:
                handle.write(b"\0")
            handle.seek(0)
            self._lock(handle)
        except OSError as exc:
            handle.close()
            raise InstanceAlreadyRunning(
                f"另一个 AI 共创室实例正在使用数据库：{self.database_path}"
            ) from exc
        self._handle = handle
        self._held = True
        diagnostic = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": int(time.time() * 1000),
            **(metadata or {}),
        }
        try:
            encoded = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True).encode("utf-8")
            handle.seek(1)
            handle.truncate(1)
            handle.write(encoded[:4096])
        except OSError:
            self.release()
            raise
        return self

    def _open_unaliased_lock_file(self) -> BinaryIO:
        """Open the owner lock without following a link or alias.

        The lock file is itself a security boundary: following a pre-existing
        symlink (or accepting a hard link) could make startup write diagnostic
        metadata into an unrelated user file.  Create-if-absent is attempted
        first so a concurrent symlink cannot win the creation race; existing
        files are checked both before and after opening.
        """

        flags = os.O_RDWR | int(getattr(os, "O_BINARY", 0) or 0)
        created = False
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    self.lock_path,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                created = True
            except FileExistsError:
                self._assert_unaliased_lock_path()
                descriptor = os.open(self.lock_path, flags)

            self._assert_unaliased_lock_path()
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or int(opened.st_nlink) != 1:
                raise RuntimeError(
                    "Database owner lock must be an independent regular file"
                )
            path_stat = self.lock_path.stat()
            if (
                int(opened.st_dev) != int(path_stat.st_dev)
                or int(opened.st_ino) != int(path_stat.st_ino)
            ):
                raise RuntimeError(
                    "Database owner lock changed identity while opening"
                )
            return os.fdopen(descriptor, "r+b", buffering=0, closefd=True)
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            if created:
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass
            raise

    def _assert_unaliased_lock_path(self) -> None:
        try:
            metadata = self.lock_path.lstat()
        except FileNotFoundError:
            raise RuntimeError("Database owner lock disappeared while opening")
        except OSError as exc:
            raise RuntimeError("Cannot inspect database owner lock") from exc
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or bool(reparse_flag and attributes & reparse_flag)
        ):
            raise RuntimeError("Database owner lock may not be a symlink or reparse point")
        if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
            raise RuntimeError("Database owner lock must be an independent regular file")

    def assert_held_for(self, database_path: str | Path) -> None:
        requested_path = Path(database_path).expanduser()
        if os.path.lexists(os.fspath(requested_path)):
            self._assert_database_path_identity(requested_path)
        requested = requested_path.resolve()
        if not self.held or requested != self.database_path:
            raise RuntimeError("数据库恢复要求当前进程持有匹配的实例所有权锁")

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            self._held = False
            return
        try:
            if self._held and not handle.closed:
                handle.seek(0)
                self._unlock(handle)
        finally:
            self._held = False
            self._handle = None
            handle.close()

    def __enter__(self) -> "DatabaseInstanceOwner":
        return self.acquire()

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()

    @staticmethod
    def _lock(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
