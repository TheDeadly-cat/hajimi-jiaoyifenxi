from __future__ import annotations

import argparse
import functools
import hashlib
import json
import ntpath
import os
import re
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


PLAN_VERSION = "formal_source_promotion_plan_v1"
PROJECT_PREFIX = "ai_collaboration_studio/"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_NATIVE_PATH_TYPE = type(Path())

_COPY_CHUNK_SIZE = 1024 * 1024
_MAX_FILE_BYTES = 128 * 1024 * 1024
_MAX_CHANGED_PATHS = 2_048
_MAX_GIT_CONTROL_OUTPUT = 8 * 1024 * 1024
_MAX_GIT_DIFF_OUTPUT = 64 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 60
_PLAN_TIMEOUT_SECONDS = 300
_ALLOWED_FILE_MODES = frozenset({"100644", "100755"})
_SAFE_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
_FORBIDDEN_DIRECTORY_NAMES = frozenset({
    ".git",
    ".npm-cache",
    ".pytest_cache",
    "__pycache__",
    "backups",
    "dist",
    "logs",
    "node_modules",
    "runtime",
    "secrets",
})
_SECRET_EXACT_NAMES = frozenset({
    ".env",
    ".env.local",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "credential.json",
    "credentials.yml",
    "credentials.yaml",
    "secrets.json",
    "secret.json",
    "secrets.yml",
    "secrets.yaml",
    "secret.yml",
    "secret.yaml",
    "token.json",
    "tokens.json",
    "api_tokens.json",
    "apikeys.json",
    "oauth.json",
    "auth.json",
    "service-account.json",
    "service_account.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
})
_SECRET_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
)
_DATABASE_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    "-wal",
    "-shm",
    "-journal",
)
_WINDOWS_RESERVED_NAMES = frozenset({
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
})
_WINDOWS_LOCAL_DRIVE_TYPES = frozenset({3})


class FormalSourcePromotionPlanError(RuntimeError):
    """Raised when a formal-source comparison cannot be proven safe."""


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    object_type: str
    oid: str
    path: str


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_before_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise FormalSourcePromotionPlanError(
            "Promotion planning exceeded its aggregate time limit"
        )


@functools.lru_cache(maxsize=1)
def _git_executable_binding() -> tuple[Path, tuple[int, int, int, int, int, int]]:
    try:
        repository_root = REPOSITORY_ROOT.resolve(strict=True)
    except OSError as exc:
        raise FormalSourcePromotionPlanError(
            "The fixed reviewed repository is unavailable"
        ) from exc
    executable_name = "git.exe" if os.name == "nt" else "git"
    access_mode = os.F_OK if os.name == "nt" else os.F_OK | os.X_OK
    for raw_entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = raw_entry.strip().strip('"')
        if not entry:
            continue
        search_directory = Path(entry)
        if not search_directory.is_absolute():
            continue
        lexical_directory = Path(os.path.abspath(os.fspath(search_directory)))
        raw_directory = os.fspath(lexical_directory)
        if raw_directory.startswith(("\\\\", "//")):
            continue
        try:
            directory_metadata = lexical_directory.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(directory_metadata.st_mode) or _is_reparse(
            directory_metadata
        ):
            continue
        candidate = lexical_directory / executable_name
        try:
            candidate_metadata = candidate.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(candidate_metadata.st_mode) or _is_reparse(
            candidate_metadata
        ):
            continue
        try:
            resolved_directory = lexical_directory.resolve(strict=True)
        except OSError:
            continue
        if resolved_directory != lexical_directory:
            continue
        try:
            candidate.relative_to(repository_root)
        except ValueError:
            inside_repository = False
        else:
            inside_repository = True
        if inside_repository or not os.access(candidate, access_mode):
            continue
        signature = (
            int(candidate_metadata.st_dev),
            int(candidate_metadata.st_ino),
            int(candidate_metadata.st_mode),
            int(candidate_metadata.st_size),
            int(getattr(candidate_metadata, "st_mtime_ns", 0)),
            int(getattr(candidate_metadata, "st_ctime_ns", 0)),
        )
        return candidate, signature
    raise FormalSourcePromotionPlanError(
        "No trusted Git executable is available outside the reviewed repository"
    )


def _git_executable_path() -> Path:
    executable, expected_signature = _git_executable_binding()
    try:
        metadata = executable.lstat()
        parent_metadata = executable.parent.lstat()
        resolved_parent = executable.parent.resolve(strict=True)
        repository_root = REPOSITORY_ROOT.resolve(strict=True)
    except OSError as exc:
        raise FormalSourcePromotionPlanError("Git is unavailable") from exc
    current_signature = (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", 0)),
        int(getattr(metadata, "st_ctime_ns", 0)),
    )
    try:
        executable.relative_to(repository_root)
    except ValueError:
        inside_repository = False
    else:
        inside_repository = True
    if (
        current_signature != expected_signature
        or not stat.S_ISREG(metadata.st_mode)
        or _is_reparse(metadata)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or _is_reparse(parent_metadata)
        or resolved_parent != executable.parent
        or inside_repository
    ):
        raise FormalSourcePromotionPlanError(
            "Git must remain one trusted executable outside the reviewed repository"
        )
    return executable


def _git_environment(*, attribute_source: str | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.upper().startswith("GIT_"):
            environment.pop(key, None)
    environment.update({
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    })
    if attribute_source is not None:
        environment["GIT_ATTR_SOURCE"] = attribute_source
    return environment


def _run_git(
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    allowed_returncodes: frozenset[int] = frozenset({0}),
    attribute_source: str | None = None,
    max_stdout_bytes: int = _MAX_GIT_CONTROL_OUTPUT,
) -> subprocess.CompletedProcess[bytes]:
    command = [
        os.fspath(_git_executable_path()),
        "--no-pager",
        "-C",
        os.fspath(REPOSITORY_ROOT),
        "-c",
        "core.attributesFile=",
        *arguments,
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            env=_git_environment(attribute_source=attribute_source),
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError as exc:
        raise FormalSourcePromotionPlanError("Git is unavailable") from exc

    writer_error: list[BaseException] = []

    def write_input() -> None:
        assert process.stdin is not None
        assert input_bytes is not None
        try:
            process.stdin.write(input_bytes)
        except BrokenPipeError:
            pass
        except BaseException as exc:  # pragma: no cover - platform pipe failure
            writer_error.append(exc)
        finally:
            process.stdin.close()

    input_writer: threading.Thread | None = None
    if input_bytes is not None:
        input_writer = threading.Thread(target=write_input, daemon=True)
        input_writer.start()

    chunks: list[bytes] = []
    total = 0
    timer, expired = _arm_process_timeout(process)
    limit_exceeded = False
    try:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_stdout_bytes:
                limit_exceeded = True
                process.kill()
                break
            chunks.append(chunk)
        returncode = process.wait()
    except OSError as exc:
        process.kill()
        process.wait()
        raise FormalSourcePromotionPlanError("Git output could not be read safely") from exc
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if process.stdin is not None and input_writer is None:
            process.stdin.close()
        if input_writer is not None:
            input_writer.join(timeout=1)

    if limit_exceeded:
        raise FormalSourcePromotionPlanError("Git control output exceeds its closed limit")
    if expired.is_set():
        raise FormalSourcePromotionPlanError("Git query timed out")
    if writer_error:
        raise FormalSourcePromotionPlanError("Git input could not be written safely")
    if returncode not in allowed_returncodes:
        raise FormalSourcePromotionPlanError(
            "Git rejected an immutable promotion-plan query"
        )
    return subprocess.CompletedProcess(
        args=command,
        returncode=returncode,
        stdout=b"".join(chunks),
        stderr=b"",
    )


def _arm_process_timeout(
    process: subprocess.Popen[bytes],
) -> tuple[threading.Timer, threading.Event]:
    expired = threading.Event()

    def terminate() -> None:
        expired.set()
        try:
            process.kill()
        except OSError:
            pass

    timer = threading.Timer(_GIT_TIMEOUT_SECONDS, terminate)
    timer.daemon = True
    timer.start()
    return timer, expired


def _run_git_bounded_stdout(
    arguments: list[str],
    *,
    max_stdout_bytes: int,
) -> bytes:
    return _run_git(
        arguments,
        max_stdout_bytes=max_stdout_bytes,
    ).stdout


def _decode_single_git_path(value: bytes, *, label: str) -> Path:
    try:
        text = value.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise FormalSourcePromotionPlanError(f"{label} is invalid") from exc
    if not text:
        raise FormalSourcePromotionPlanError(f"{label} is empty")
    return Path(text)


def _validate_repository_context() -> tuple[
    Path,
    tuple[int, int],
    tuple[Path, ...],
]:
    try:
        expected = REPOSITORY_ROOT.resolve(strict=True)
    except OSError as exc:
        raise FormalSourcePromotionPlanError(
            "The fixed reviewed repository is unavailable"
        ) from exc
    top = _decode_single_git_path(
        _run_git(["rev-parse", "--show-toplevel"]).stdout,
        label="Git top-level path",
    )
    git_dir = _decode_single_git_path(
        _run_git(["rev-parse", "--absolute-git-dir"]).stdout,
        label="Git directory path",
    )
    try:
        resolved_top = top.resolve(strict=True)
        resolved_git_dir = git_dir.resolve(strict=True)
        top_metadata = resolved_top.lstat()
        git_metadata = resolved_git_dir.lstat()
    except OSError as exc:
        raise FormalSourcePromotionPlanError(
            "The fixed reviewed repository identity cannot be inspected"
        ) from exc
    expected_git_entry = expected / ".git"
    try:
        expected_git_metadata = expected_git_entry.lstat()
        expected_git_directory = expected_git_entry.resolve(strict=True)
    except OSError as exc:
        raise FormalSourcePromotionPlanError(
            "The fixed checkout must contain its own Git directory"
        ) from exc
    if resolved_top != expected or resolved_git_dir != expected_git_directory:
        raise FormalSourcePromotionPlanError(
            "Git did not resolve to the fixed reviewed repository"
        )
    if (
        not stat.S_ISDIR(top_metadata.st_mode)
        or not stat.S_ISDIR(git_metadata.st_mode)
        or not stat.S_ISDIR(expected_git_metadata.st_mode)
        or _is_reparse(top_metadata)
        or _is_reparse(git_metadata)
        or _is_reparse(expected_git_metadata)
    ):
        raise FormalSourcePromotionPlanError(
            "The reviewed repository or Git directory is aliased"
        )
    forbidden_overrides = (
        resolved_git_dir / "info" / "attributes",
        resolved_git_dir / "objects" / "info" / "alternates",
        resolved_git_dir / "objects" / "info" / "http-alternates",
    )
    if any(os.path.lexists(os.fspath(path)) for path in forbidden_overrides):
        raise FormalSourcePromotionPlanError(
            "Repository-local attributes or object alternates are unsupported"
        )
    return (
        resolved_top,
        (int(top_metadata.st_dev), int(top_metadata.st_ino)),
        forbidden_overrides,
    )


def _revalidate_repository_context(
    expected_root: Path,
    expected_identity: tuple[int, int],
    forbidden_overrides: tuple[Path, ...],
) -> None:
    if any(os.path.lexists(os.fspath(path)) for path in forbidden_overrides):
        raise FormalSourcePromotionPlanError(
            "Repository-local attributes or object alternates appeared during inspection"
        )
    current = _decode_single_git_path(
        _run_git(["rev-parse", "--show-toplevel"]).stdout,
        label="Git top-level path",
    )
    try:
        resolved = current.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as exc:
        raise FormalSourcePromotionPlanError(
            "The reviewed repository changed while the plan was built"
        ) from exc
    if (
        resolved != expected_root
        or (int(metadata.st_dev), int(metadata.st_ino)) != expected_identity
        or _is_reparse(metadata)
    ):
        raise FormalSourcePromotionPlanError(
            "The reviewed repository changed while the plan was built"
        )


def _object_format() -> tuple[str, int]:
    result = _run_git(["rev-parse", "--show-object-format"])
    try:
        value = result.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise FormalSourcePromotionPlanError("Git object format is invalid") from exc
    if value == "sha1":
        return value, 40
    if value == "sha256":
        return value, 64
    raise FormalSourcePromotionPlanError("Git object format is unsupported")


def _validate_commit_oid(value: Any, *, label: str, oid_length: int) -> str:
    if type(value) is not str or re.fullmatch(f"[0-9a-f]{{{oid_length}}}", value) is None:
        raise FormalSourcePromotionPlanError(
            f"{label} must be one exact lowercase full commit object ID"
        )
    result = _run_git(
        ["rev-parse", "--verify", "--end-of-options", f"{value}^{{commit}}"]
    )
    try:
        resolved = result.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise FormalSourcePromotionPlanError(f"{label} could not be verified") from exc
    if resolved != value:
        raise FormalSourcePromotionPlanError(
            f"{label} must identify the commit directly, not an alias or tag"
        )
    return value


def _require_linear_commits(base_commit: str, tip_commit: str) -> None:
    if base_commit == tip_commit:
        raise FormalSourcePromotionPlanError("base_commit and tip_commit must differ")
    result = _run_git(
        ["merge-base", "--is-ancestor", base_commit, tip_commit],
        allowed_returncodes=frozenset({0, 1}),
    )
    if result.returncode != 0:
        raise FormalSourcePromotionPlanError(
            "base_commit must be an ancestor of tip_commit"
        )


def _decode_git_path(value: bytes) -> str:
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FormalSourcePromotionPlanError(
            "Promotion paths must be canonical UTF-8"
        ) from exc


def _changed_project_paths(base_commit: str, tip_commit: str) -> list[tuple[str, str]]:
    payload = _run_git_bounded_stdout([
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--name-status",
        "-z",
        base_commit,
        tip_commit,
        "--",
        f":(top,literal){PROJECT_PREFIX}",
    ], max_stdout_bytes=_MAX_GIT_DIFF_OUTPUT)
    if not payload or not payload.endswith(b"\x00"):
        raise FormalSourcePromotionPlanError(
            "The immutable commit range has no closed project-path delta"
        )
    fields = payload[:-1].split(b"\x00")
    if len(fields) % 2 != 0:
        raise FormalSourcePromotionPlanError("Git returned an ambiguous path delta")
    rows: list[tuple[str, str]] = []
    for index in range(0, len(fields), 2):
        try:
            status_value = fields[index].decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise FormalSourcePromotionPlanError("Git change status is invalid") from exc
        path = _decode_git_path(fields[index + 1])
        if len(fields[index + 1]) > 4096:
            raise FormalSourcePromotionPlanError(
                "Promotion path exceeds the closed byte-length limit"
            )
        if status_value not in {"A", "M"}:
            raise FormalSourcePromotionPlanError(
                "Only added or modified regular files can enter a promotion plan"
            )
        rows.append((status_value, path))
        if len(rows) > _MAX_CHANGED_PATHS:
            raise FormalSourcePromotionPlanError(
                "The promotion delta exceeds the closed path-count limit"
            )
    return rows


def _is_secret_filename(name: str) -> bool:
    lowered = name.casefold()
    if lowered in _SAFE_ENV_TEMPLATES:
        return False
    if lowered.startswith(".env") or lowered in _SECRET_EXACT_NAMES:
        return True
    if lowered.endswith(_SECRET_SUFFIXES):
        return True
    if lowered.endswith((".secret", ".secret.json", ".secret.yml", ".secret.yaml")):
        return True
    if lowered in {"api_key.txt", "apikey.txt", "access_token.txt", "token.txt"}:
        return True
    return "密钥" in name or "私钥" in name


def _validate_windows_component(component: str) -> None:
    if component != unicodedata.normalize("NFC", component):
        raise FormalSourcePromotionPlanError(
            "Promotion paths must use NFC-normalized components"
        )
    if any(character in '<>"|?*' for character in component):
        raise FormalSourcePromotionPlanError(
            "Promotion paths may not contain Windows-forbidden characters"
        )
    if component.endswith((" ", ".")):
        raise FormalSourcePromotionPlanError(
            "Promotion paths may not contain Windows-trimmed components"
        )
    device_stem = component.split(".", 1)[0].rstrip(" ").casefold()
    if device_stem in _WINDOWS_RESERVED_NAMES:
        raise FormalSourcePromotionPlanError(
            "Promotion paths may not contain Windows device names"
        )
    if re.search(r"~[0-9]+(?:\.|$)", component, flags=re.IGNORECASE):
        raise FormalSourcePromotionPlanError(
            "Promotion paths may not use DOS short-name aliases"
        )


def _validate_promotion_path(repo_path: Any) -> str:
    if type(repo_path) is not str or not repo_path.startswith(PROJECT_PREFIX):
        raise FormalSourcePromotionPlanError(
            "Every promotion path must use the fixed project prefix"
        )
    relative = repo_path[len(PROJECT_PREFIX):]
    if not relative or "\\" in relative or ":" in relative or "\x00" in relative:
        raise FormalSourcePromotionPlanError("Promotion path is not portable")
    if any(ord(character) < 32 or ord(character) == 127 for character in relative):
        raise FormalSourcePromotionPlanError("Promotion path contains control characters")
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or pure.as_posix() != relative
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise FormalSourcePromotionPlanError("Promotion path escapes its project root")
    for index, component in enumerate(pure.parts):
        _validate_windows_component(component)
        lowered = component.casefold()
        if lowered in _FORBIDDEN_DIRECTORY_NAMES:
            raise FormalSourcePromotionPlanError(
                "Promotion path enters an excluded runtime or generated directory"
            )
        if lowered.endswith(_DATABASE_SUFFIXES) or _is_secret_filename(component):
            raise FormalSourcePromotionPlanError(
                "Promotion path names runtime, database, generated, or secret material"
            )
        if lowered in _SAFE_ENV_TEMPLATES and index != len(pure.parts) - 1:
            raise FormalSourcePromotionPlanError(
                "A safe environment template may only be the target leaf"
            )
    leaf = pure.parts[-1]
    lowered_leaf = leaf.casefold()
    if (
        lowered_leaf.endswith(".pyc")
    ):
        raise FormalSourcePromotionPlanError(
            "Promotion path names runtime, database, generated, or secret material"
        )
    return relative


def _validate_all_paths(rows: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    validated: list[tuple[str, str, str]] = []
    exact_seen: set[str] = set()
    folded_seen: set[str] = set()
    for status_value, repo_path in rows:
        relative = _validate_promotion_path(repo_path)
        collision_key = unicodedata.normalize("NFC", repo_path).casefold()
        if repo_path in exact_seen or collision_key in folded_seen:
            raise FormalSourcePromotionPlanError(
                "Promotion paths contain a duplicate or case-insensitive collision"
            )
        exact_seen.add(repo_path)
        folded_seen.add(collision_key)
        validated.append((status_value, repo_path, relative))
    return sorted(validated, key=lambda row: row[1])


def _tree_blob_entry(commit: str, path: str) -> _TreeEntry | None:
    result = _run_git([
        "ls-tree",
        "-z",
        "--full-tree",
        commit,
        "--",
        f":(top,literal){path}",
    ])
    if not result.stdout:
        return None
    if not result.stdout.endswith(b"\x00"):
        raise FormalSourcePromotionPlanError("Git tree entry is not NUL-terminated")
    records = result.stdout[:-1].split(b"\x00")
    if len(records) != 1 or b"\t" not in records[0]:
        raise FormalSourcePromotionPlanError("Git tree path is ambiguous")
    metadata, raw_path = records[0].split(b"\t", 1)
    try:
        mode, object_type, oid = metadata.decode("ascii", errors="strict").split(" ")
    except (UnicodeDecodeError, ValueError) as exc:
        raise FormalSourcePromotionPlanError("Git tree metadata is invalid") from exc
    returned_path = _decode_git_path(raw_path)
    if returned_path != path:
        raise FormalSourcePromotionPlanError("Git tree returned a different path")
    return _TreeEntry(mode=mode, object_type=object_type, oid=oid, path=returned_path)


def _validate_tree_entries(
    rows: list[tuple[str, str, str]],
    *,
    base_commit: str,
    tip_commit: str,
    oid_length: int,
    deadline: float,
) -> list[tuple[str, str, str, _TreeEntry | None, _TreeEntry]]:
    result: list[tuple[str, str, str, _TreeEntry | None, _TreeEntry]] = []
    for status_value, repo_path, relative in rows:
        _require_before_deadline(deadline)
        base = _tree_blob_entry(base_commit, repo_path)
        tip = _tree_blob_entry(tip_commit, repo_path)
        if tip is None or tip.object_type != "blob" or tip.mode not in _ALLOWED_FILE_MODES:
            raise FormalSourcePromotionPlanError(
                "Every tip path must be one supported regular-file blob"
            )
        if re.fullmatch(f"[0-9a-f]{{{oid_length}}}", tip.oid) is None:
            raise FormalSourcePromotionPlanError("Tip blob object ID is invalid")
        if status_value == "A":
            if base is not None:
                raise FormalSourcePromotionPlanError(
                    "Added path unexpectedly exists in the base commit"
                )
        else:
            if (
                base is None
                or base.object_type != "blob"
                or base.mode not in _ALLOWED_FILE_MODES
                or re.fullmatch(f"[0-9a-f]{{{oid_length}}}", base.oid) is None
            ):
                raise FormalSourcePromotionPlanError(
                    "Modified path lacks one supported base blob"
                )
            if base.mode != tip.mode or base.oid == tip.oid:
                raise FormalSourcePromotionPlanError(
                    "Mode-only and ambiguous modifications are not supported"
                )
        result.append((status_value, repo_path, relative, base, tip))
    return result


def _validate_attributes(
    paths: list[str],
    *,
    tip_commit: str,
    deadline: float,
) -> None:
    expected_paths = {path: set() for path in paths}
    for start in range(0, len(paths), 128):
        _require_before_deadline(deadline)
        chunk_paths = paths[start:start + 128]
        input_bytes = b"".join(
            path.encode("utf-8") + b"\x00" for path in chunk_paths
        )
        result = _run_git(
            [
                "check-attr",
                "-z",
                f"--source={tip_commit}",
                "--stdin",
                "filter",
                "ident",
                "working-tree-encoding",
            ],
            input_bytes=input_bytes,
            max_stdout_bytes=2 * 1024 * 1024,
        )
        if not result.stdout.endswith(b"\x00"):
            raise FormalSourcePromotionPlanError("Git attribute result is not closed")
        fields = result.stdout[:-1].split(b"\x00")
        if len(fields) != len(chunk_paths) * 3 * 3:
            raise FormalSourcePromotionPlanError("Git attribute result is incomplete")
        for index in range(0, len(fields), 3):
            path = _decode_git_path(fields[index])
            try:
                attribute = fields[index + 1].decode("ascii", errors="strict")
                value = fields[index + 2].decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise FormalSourcePromotionPlanError(
                    "Git attribute result is invalid"
                ) from exc
            if path not in expected_paths or attribute not in {
                "filter",
                "ident",
                "working-tree-encoding",
            }:
                raise FormalSourcePromotionPlanError("Git attribute scope changed")
            if attribute in expected_paths[path]:
                raise FormalSourcePromotionPlanError(
                    "Git attribute result is duplicated"
                )
            expected_paths[path].add(attribute)
            if value not in {"unspecified", "unset"}:
                raise FormalSourcePromotionPlanError(
                    "Custom filters, ident conversion, and working-tree encodings are not supported"
                )
    if any(
        values != {"filter", "ident", "working-tree-encoding"}
        for values in expected_paths.values()
    ):
        raise FormalSourcePromotionPlanError("Git attribute result is incomplete")


def _blob_record(entry: _TreeEntry) -> dict[str, Any]:
    size_result = _run_git(["cat-file", "-s", entry.oid])
    try:
        expected_size = int(size_result.stdout.decode("ascii", errors="strict").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise FormalSourcePromotionPlanError("Git blob size is invalid") from exc
    if expected_size < 0 or expected_size > _MAX_FILE_BYTES:
        raise FormalSourcePromotionPlanError("Git blob exceeds the promotion size limit")
    command = [
        os.fspath(_git_executable_path()),
        "--no-pager",
        "-C",
        os.fspath(REPOSITORY_ROOT),
        "-c",
        "core.attributesFile=",
        "cat-file",
        "blob",
        entry.oid,
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError as exc:
        raise FormalSourcePromotionPlanError("Git is unavailable") from exc
    digest = hashlib.sha256()
    actual_size = 0
    timer, expired = _arm_process_timeout(process)
    try:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            actual_size += len(chunk)
            if actual_size > expected_size or actual_size > _MAX_FILE_BYTES:
                process.kill()
                raise FormalSourcePromotionPlanError(
                    "Git blob exceeded its preflight size"
                )
            digest.update(chunk)
        returncode = process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
    if expired.is_set():
        raise FormalSourcePromotionPlanError("Git blob query timed out")
    if returncode != 0 or actual_size != expected_size:
        raise FormalSourcePromotionPlanError("Git blob content is incomplete")
    return {
        "mode": entry.mode,
        "git_oid": entry.oid,
        "size": actual_size,
        "sha256": digest.hexdigest(),
    }


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(flag and attributes & flag)


def _windows_drive_type(drive: str) -> int:
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_drive_type = kernel32.GetDriveTypeW
        get_drive_type.argtypes = (ctypes.c_wchar_p,)
        get_drive_type.restype = ctypes.c_uint
        return int(get_drive_type(f"{drive}\\"))
    except (AttributeError, OSError, TypeError, ValueError):
        return 0


def _reject_remote_path_before_access(raw_path: str) -> None:
    normalized = raw_path.replace("/", "\\")
    folded = normalized.casefold()
    if (
        normalized.startswith("\\\\")
        or folded.startswith("\\??\\")
        or folded.startswith("\\device\\")
    ):
        raise FormalSourcePromotionPlanError(
            "formal_source_root must use a local filesystem path"
        )
    if os.name != "nt":
        return
    drive, _tail = ntpath.splitdrive(normalized)
    if re.fullmatch(r"[A-Za-z]:", drive or "") is None:
        raise FormalSourcePromotionPlanError(
            "formal_source_root must use one local DOS drive"
        )
    if _windows_drive_type(drive) not in _WINDOWS_LOCAL_DRIVE_TYPES:
        raise FormalSourcePromotionPlanError(
            "formal_source_root may not use a remote or indeterminate drive"
        )


def _directory_signature(path: Path) -> tuple[int, int, int, int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FormalSourcePromotionPlanError(
            "formal_source_root is unavailable"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
        raise FormalSourcePromotionPlanError(
            "formal_source_root must be one non-reparse directory"
        )
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(getattr(metadata, "st_mtime_ns", 0)),
        int(getattr(metadata, "st_ctime_ns", 0)),
    )


def _formal_root(value: Any) -> tuple[Path, tuple[int, int, int, int, int]]:
    if type(value) not in {str, _NATIVE_PATH_TYPE}:
        raise FormalSourcePromotionPlanError(
            "formal_source_root must be a native string or Path"
        )
    raw = value if type(value) is str else str(value)
    if not raw or raw != raw.strip() or not os.path.isabs(raw):
        raise FormalSourcePromotionPlanError(
            "formal_source_root must be one explicit canonical absolute path"
        )
    _reject_remote_path_before_access(raw)
    requested = Path(raw)
    absolute = Path(os.path.abspath(os.fspath(requested)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if not os.path.lexists(os.fspath(current)):
            raise FormalSourcePromotionPlanError("formal_source_root is unavailable")
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise FormalSourcePromotionPlanError(
                "formal_source_root identity cannot be inspected"
            ) from exc
        if _is_reparse(metadata):
            raise FormalSourcePromotionPlanError(
                "formal_source_root may not contain a symlink or reparse point"
            )
    resolved = absolute.resolve(strict=True)
    repository = REPOSITORY_ROOT.resolve(strict=True)
    try:
        resolved.relative_to(repository)
        overlaps = True
    except ValueError:
        try:
            repository.relative_to(resolved)
            overlaps = True
        except ValueError:
            overlaps = False
    if overlaps:
        raise FormalSourcePromotionPlanError(
            "formal_source_root must be disjoint from the reviewed repository"
        )
    return resolved, _directory_signature(resolved)


def _formal_root_binding(
    root: Path,
    signature: tuple[int, int, int, int, int],
) -> dict[str, Any]:
    canonical_path = os.path.normcase(os.path.normpath(os.fspath(root)))
    return {
        "path_sha256": hashlib.sha256(canonical_path.encode("utf-8")).hexdigest(),
        "identity": {
            "device": signature[0],
            "inode": signature[1],
        },
    }


def _assert_explicit_parent_chain(
    root: Path,
    relative: str,
) -> tuple[Path, tuple[tuple[Path, tuple[int, int, int, int, int]], ...], Path | None]:
    parts = PurePosixPath(relative).parts
    current = root
    observed: list[tuple[Path, tuple[int, int, int, int, int]]] = []
    first_missing: Path | None = None
    for component in parts[:-1]:
        current /= component
        if not os.path.lexists(os.fspath(current)):
            first_missing = current
            break
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise FormalSourcePromotionPlanError(
                "A formal-source parent changed while it was inspected"
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
            raise FormalSourcePromotionPlanError(
                "A formal-source parent is not one real directory"
            )
        observed.append((current, _directory_signature(current)))
    return root.joinpath(*parts), tuple(observed), first_missing


def _revalidate_parent_chain(
    observed: tuple[tuple[Path, tuple[int, int, int, int, int]], ...],
    first_missing: Path | None,
) -> None:
    for path, expected in observed:
        if _directory_signature(path) != expected:
            raise FormalSourcePromotionPlanError(
                "A formal-source parent changed while the plan was built"
            )
    if first_missing is not None and os.path.lexists(os.fspath(first_missing)):
        raise FormalSourcePromotionPlanError(
            "A missing formal-source parent appeared while the plan was built"
        )


def _public_parent_chain(
    root: Path,
    observed: tuple[tuple[Path, tuple[int, int, int, int, int]], ...],
    first_missing: Path | None,
) -> dict[str, Any]:
    return {
        "existing": [
            {
                "path": path.relative_to(root).as_posix(),
                "identity": {
                    "device": signature[0],
                    "inode": signature[1],
                },
            }
            for path, signature in observed
        ],
        "first_missing": (
            None
            if first_missing is None
            else first_missing.relative_to(root).as_posix()
        ),
    }


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", 0)),
        int(getattr(metadata, "st_ctime_ns", 0)),
        int(metadata.st_nlink),
    )


def _read_descriptor_pass(descriptor: int, *, collect: bool) -> tuple[int, str, bytes | None]:
    digest = hashlib.sha256()
    total = 0
    chunks: list[bytes] | None = [] if collect else None
    while True:
        chunk = os.read(descriptor, _COPY_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_FILE_BYTES:
            raise FormalSourcePromotionPlanError(
                "A formal-source file exceeds the promotion size limit"
            )
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
    return total, digest.hexdigest(), b"".join(chunks) if chunks is not None else None


def _read_formal_file_twice(path: Path) -> dict[str, Any] | None:
    if not os.path.lexists(os.fspath(path)):
        return None
    try:
        before = path.lstat()
    except OSError as exc:
        raise FormalSourcePromotionPlanError(
            "A formal-source path changed before it could be opened"
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or _is_reparse(before)
        or int(before.st_nlink) != 1
    ):
        raise FormalSourcePromotionPlanError(
            "Every existing formal-source target must be an independent regular file"
        )
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
    flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FormalSourcePromotionPlanError(
            "A formal-source file could not be opened safely"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not os.path.samestat(before, opened)
            or not stat.S_ISREG(opened.st_mode)
            or int(opened.st_nlink) != 1
        ):
            raise FormalSourcePromotionPlanError(
                "A formal-source file identity changed while it was opened"
            )
        first_size, first_sha256, payload = _read_descriptor_pass(
            descriptor,
            collect=True,
        )
        os.lseek(descriptor, 0, os.SEEK_SET)
        second_size, second_sha256, _unused = _read_descriptor_pass(
            descriptor,
            collect=False,
        )
        try:
            after = path.lstat()
        except OSError as exc:
            raise FormalSourcePromotionPlanError(
                "A formal-source file disappeared while it was inspected"
            ) from exc
        final_descriptor = os.fstat(descriptor)
        if (
            not os.path.samestat(opened, final_descriptor)
            or not os.path.samestat(opened, after)
            or _is_reparse(after)
            or int(after.st_nlink) != 1
            or int(final_descriptor.st_nlink) != 1
            or _file_signature(before) != _file_signature(after)
            or _file_signature(opened) != _file_signature(final_descriptor)
            or first_size != second_size
            or first_sha256 != second_sha256
            or first_size != int(opened.st_size)
        ):
            raise FormalSourcePromotionPlanError(
                "A formal-source file changed while it was inspected"
            )
        assert payload is not None
        return {
            "exists": True,
            "mode": int(opened.st_mode),
            "size": first_size,
            "sha256": first_sha256,
            "identity": {
                "device": int(opened.st_dev),
                "inode": int(opened.st_ino),
            },
            "_payload": payload,
        }
    finally:
        os.close(descriptor)


def _git_blob_oid(payload: bytes, *, object_format: str) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    if object_format == "sha1":
        digest = hashlib.sha1(usedforsecurity=False)
    elif object_format == "sha256":
        digest = hashlib.sha256()
    else:  # pragma: no cover - object format is validated before file access
        raise FormalSourcePromotionPlanError("Git object format is unsupported")
    digest.update(header)
    digest.update(payload)
    return digest.hexdigest()


def _target_git_oids(payload: bytes, *, object_format: str) -> dict[str, str | None]:
    raw_oid = _git_blob_oid(payload, object_format=object_format)
    normalized_oid: str | None = None
    if b"\r\n" in payload:
        try:
            decoded = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            decoded = ""
        if decoded and not any(
            (ord(character) < 32 and character not in "\t\n\r")
            or ord(character) == 127
            for character in decoded
        ):
            normalized = payload.replace(b"\r\n", b"\n")
            normalized_oid = _git_blob_oid(
                normalized,
                object_format=object_format,
            )
    return {
        "raw_git_oid": raw_oid,
        "strict_crlf_normalized_git_oid": normalized_oid,
    }


def _revalidate_target_observation(
    path: Path,
    expected: dict[str, Any] | None,
    parent_chain: tuple[tuple[Path, tuple[int, int, int, int, int]], ...],
    first_missing_parent: Path | None,
) -> None:
    _revalidate_parent_chain(parent_chain, first_missing_parent)
    observed = _read_formal_file_twice(path)
    if expected is None:
        if observed is not None:
            raise FormalSourcePromotionPlanError(
                "A formal-source target appeared after its plan entry was built"
            )
        return
    if observed is None:
        raise FormalSourcePromotionPlanError(
            "A formal-source target disappeared after its plan entry was built"
        )
    observed.pop("_payload")
    if observed != expected:
        raise FormalSourcePromotionPlanError(
            "A formal-source target changed after its plan entry was built"
        )


def _seal_plan(body: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(body)
    sealed["plan_sha256"] = _canonical_sha256(body)
    return sealed


def build_formal_source_promotion_plan(
    *,
    formal_source_root: str | Path,
    base_commit: str,
    tip_commit: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + _PLAN_TIMEOUT_SECONDS
    repository_root, repository_identity, forbidden_git_overrides = (
        _validate_repository_context()
    )
    _require_before_deadline(deadline)
    object_format, oid_length = _object_format()
    base = _validate_commit_oid(base_commit, label="base_commit", oid_length=oid_length)
    tip = _validate_commit_oid(tip_commit, label="tip_commit", oid_length=oid_length)
    _require_linear_commits(base, tip)

    changed = _validate_all_paths(_changed_project_paths(base, tip))
    tree_rows = _validate_tree_entries(
        changed,
        base_commit=base,
        tip_commit=tip,
        oid_length=oid_length,
        deadline=deadline,
    )
    _validate_attributes(
        [row[1] for row in tree_rows],
        tip_commit=tip,
        deadline=deadline,
    )
    blob_records: dict[tuple[str, str], dict[str, Any]] = {}
    for _status, _repo_path, _relative, base_entry, tip_entry in tree_rows:
        _require_before_deadline(deadline)
        if base_entry is not None:
            base_key = (base_entry.oid, base_entry.mode)
            if base_key not in blob_records:
                blob_records[base_key] = _blob_record(base_entry)
        tip_key = (tip_entry.oid, tip_entry.mode)
        if tip_key not in blob_records:
            blob_records[tip_key] = _blob_record(tip_entry)

    formal_root, initial_root_signature = _formal_root(formal_source_root)
    entries: list[dict[str, Any]] = []
    classification_counts = {
        "already_tip": 0,
        "clean_add": 0,
        "clean_apply": 0,
        "manual_merge_required": 0,
    }
    status_counts = {"added": 0, "modified": 0}
    target_observations: list[
        tuple[
            Path,
            dict[str, Any] | None,
            tuple[tuple[Path, tuple[int, int, int, int, int]], ...],
            Path | None,
        ]
    ] = []
    for status_value, repo_path, relative, base_entry, tip_entry in tree_rows:
        _require_before_deadline(deadline)
        target_path, parent_chain, first_missing_parent = _assert_explicit_parent_chain(
            formal_root,
            relative,
        )
        target = _read_formal_file_twice(target_path)
        if target is None:
            if os.path.lexists(os.fspath(target_path)):
                raise FormalSourcePromotionPlanError(
                    "A missing formal-source target appeared during inspection"
                )
            if status_value == "A":
                classification = "clean_add"
                reason = "target_absent_and_base_absent"
            else:
                classification = "manual_merge_required"
                reason = "modified_target_missing"
            public_target: dict[str, Any] = {"exists": False}
            target_observation = None
        else:
            payload = target.pop("_payload")
            target_observation = dict(target)
            target_oids = _target_git_oids(
                payload,
                object_format=object_format,
            )
            matching_oids = {
                oid for oid in target_oids.values() if oid is not None
            }
            target.update(target_oids)
            public_target = target
            if tip_entry.oid in matching_oids:
                classification = "already_tip"
                reason = "target_matches_tip"
            elif (
                status_value == "M"
                and base_entry is not None
                and base_entry.oid in matching_oids
            ):
                classification = "clean_apply"
                reason = "target_matches_base"
            elif status_value == "A":
                classification = "manual_merge_required"
                reason = "added_target_already_occupied"
            else:
                classification = "manual_merge_required"
                reason = "target_differs_from_base_and_tip"
        classification_counts[classification] += 1
        _revalidate_parent_chain(parent_chain, first_missing_parent)
        target_observations.append((
            target_path,
            target_observation,
            parent_chain,
            first_missing_parent,
        ))
        status_counts["added" if status_value == "A" else "modified"] += 1
        entries.append({
            "path": relative,
            "change": "add" if status_value == "A" else "modify",
            "base": (
                None
                if base_entry is None
                else blob_records[(base_entry.oid, base_entry.mode)]
            ),
            "tip": blob_records[(tip_entry.oid, tip_entry.mode)],
            "target": public_target,
            "target_parent_chain": _public_parent_chain(
                formal_root,
                parent_chain,
                first_missing_parent,
            ),
            "classification": classification,
            "reason": reason,
        })

    for target_path, expected, parent_chain, first_missing_parent in target_observations:
        _require_before_deadline(deadline)
        _revalidate_target_observation(
            target_path,
            expected,
            parent_chain,
            first_missing_parent,
        )
    if _directory_signature(formal_root) != initial_root_signature:
        raise FormalSourcePromotionPlanError(
            "formal_source_root changed while the plan was built"
        )
    _revalidate_repository_context(
        repository_root,
        repository_identity,
        forbidden_git_overrides,
    )
    _require_before_deadline(deadline)
    if sum(classification_counts.values()) != len(entries):
        raise FormalSourcePromotionPlanError("Promotion classification counts are invalid")
    review_eligible = classification_counts["manual_merge_required"] == 0
    body = {
        "version": PLAN_VERSION,
        "ok": True,
        "repository": {
            "object_format": object_format,
            "base_commit": base,
            "tip_commit": tip,
            "project_prefix": PROJECT_PREFIX,
        },
        "scope": {
            "changed_path_count": len(entries),
            "target_path_count": len(entries),
            "target_extra_paths_evaluated": False,
        },
        "formal_root_binding": _formal_root_binding(
            formal_root,
            initial_root_signature,
        ),
        "counts": {
            "changes": status_counts,
            "classifications": classification_counts,
        },
        "entries": entries,
        "separate_write_review": {
            "eligible": review_eligible,
            "atomic_snapshot": False,
            "valid_as_write_precondition": False,
            "requires_fresh_locked_preview": True,
        },
        "writes_authorized": False,
        "safety": {
            "comparison_only": True,
            "target_enumeration": False,
            "filesystem_writes": False,
            "network_calls": False,
            "database_access": False,
            "provider_calls": False,
            "futu_calls": False,
            "trading_execution": False,
            "automatic_merge": False,
            "atomic_snapshot": False,
        },
    }
    return _seal_plan(body)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a sealed, read-only formal-source promotion comparison"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    compare = subparsers.add_parser(
        "compare",
        help="compare an exact immutable commit delta with one formal source root",
    )
    compare.add_argument("--formal-source-root", required=True)
    compare.add_argument("--base-commit", required=True)
    compare.add_argument("--tip-commit", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_formal_source_promotion_plan(
            formal_source_root=args.formal_source_root,
            base_commit=args.base_commit,
            tip_commit=args.tip_commit,
        )
    except FormalSourcePromotionPlanError as exc:
        failure = {
            "version": PLAN_VERSION,
            "ok": False,
            "error": str(exc),
            "writes_authorized": False,
        }
        print(_canonical_json(failure), file=sys.stderr)
        return 2
    print(_canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
