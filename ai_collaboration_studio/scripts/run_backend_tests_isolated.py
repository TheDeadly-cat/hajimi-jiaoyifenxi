from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch


BACKEND_TEST_LAYER_MANIFEST_VERSION = "backend_test_layers_v2"
BACKEND_TEST_LAYER_IDS = ("migration", "core", "domains", "delivery", "full")
BACKEND_TEST_LAYER_MANIFEST_PATH = Path(__file__).with_name(
    "backend_test_layers.json"
)
_MANIFEST_KEYS = {"version", "layers"}
_LAYER_KEYS = {
    "id",
    "description",
    "selection",
    "tests",
    "start_directory",
    "pattern",
}
_TEST_MODULE_PATTERN = re.compile(r"^tests\.test_[a-z0-9_]+$")
_PROVIDER_KEY_NAMES = (
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ARK_API_KEY",
    "DOUBAO_API_KEY",
    "GLM_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPUAI_API_KEY",
)
_PROXY_KEY_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_FORBIDDEN_LOOPBACK_PORTS = frozenset({8770, 11111})
_OFFLINE_SENTINEL_PORT = 1
_CHILD_NETWORK_BLOCK_EXIT_CODE = 86
_CHILD_BOOTSTRAP_DIR = Path(__file__).with_name("isolated_test_bootstrap")
_CHILD_NETWORK_AUDIT_FILE_NAME = "child-network-blocks.log"
_CHILD_NETWORK_AUDIT_ENV = "AI_STUDIO_TEST_NETWORK_AUDIT_FILE"


class BackendTestLayerError(ValueError):
    """Raised when a test-layer manifest or CLI selection is ambiguous."""


class BackendTestNetworkIsolationError(AssertionError):
    """Raised before a backend test can reach a non-isolated network target."""


class BackendTestNetworkAudit:
    """Thread-safe evidence collected by the process-wide socket guard."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.allowed_loopback_connections = 0
        self.simulated_offline_connections = 0
        self.blocked_attempts: list[str] = []

    def record_allowed(self) -> None:
        with self._lock:
            self.allowed_loopback_connections += 1

    def record_simulated_offline(self) -> None:
        with self._lock:
            self.simulated_offline_connections += 1

    def record_blocked(self, detail: str) -> None:
        with self._lock:
            if len(self.blocked_attempts) < 20:
                self.blocked_attempts.append(detail)

    @property
    def blocked_attempt_count(self) -> int:
        with self._lock:
            return len(self.blocked_attempts)

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": "backend_test_network_audit_v2",
                "allowed_loopback_connections": self.allowed_loopback_connections,
                "simulated_offline_connections": self.simulated_offline_connections,
                "blocked_attempt_count": len(self.blocked_attempts),
                "blocked_attempts": list(self.blocked_attempts),
                "formal_ports_forbidden": sorted(_FORBIDDEN_LOOPBACK_PORTS),
                "non_loopback_forbidden": True,
            }


def _canonical_network_host(value: Any) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("ascii", errors="strict").strip()
        except UnicodeError:
            return ""
    return str(value or "").strip()


def _is_loopback_host(value: Any) -> bool:
    host = _canonical_network_host(value)
    if host.casefold().rstrip(".") == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _validate_network_destination(
    address: Any,
    *,
    operation: str,
    audit: BackendTestNetworkAudit,
    fatal_exit_code: int | None = None,
) -> tuple[str, int]:
    if not isinstance(address, tuple) or len(address) < 2:
        detail = f"{operation}:non_inet_destination"
        _deny_network_access(
            audit,
            detail,
            f"isolated backend tests rejected {detail}",
            fatal_exit_code=fatal_exit_code,
        )
    host = _canonical_network_host(address[0])
    try:
        port = int(address[1])
    except (TypeError, ValueError):
        port = -1
    detail = f"{operation}:{host or '<blank>'}:{port}"
    if not _is_loopback_host(host):
        _deny_network_access(
            audit,
            detail,
            f"isolated backend tests forbid non-loopback network access: {detail}",
            fatal_exit_code=fatal_exit_code,
        )
    if port in _FORBIDDEN_LOOPBACK_PORTS:
        _deny_network_access(
            audit,
            detail,
            f"isolated backend tests forbid formal service ports: {detail}",
            fatal_exit_code=fatal_exit_code,
        )
    if not 1 <= port <= 65535:
        _deny_network_access(
            audit,
            detail,
            f"isolated backend tests rejected an invalid destination: {detail}",
            fatal_exit_code=fatal_exit_code,
        )
    return host, port


def _deny_network_access(
    audit: BackendTestNetworkAudit,
    detail: str,
    message: str,
    *,
    fatal_exit_code: int | None,
) -> None:
    audit.record_blocked(detail)
    if fatal_exit_code is not None:
        try:
            _append_child_network_block(detail)
            sys.stderr.write(
                "AI_STUDIO_TEST_NETWORK_BLOCKED " + detail + "\n"
            )
            sys.stderr.flush()
        finally:
            os._exit(fatal_exit_code)
    raise BackendTestNetworkIsolationError(message)


def _append_child_network_block(detail: str) -> None:
    raw_path = str(os.environ.get(_CHILD_NETWORK_AUDIT_ENV) or "").strip()
    if not raw_path:
        return
    path = Path(raw_path).resolve()
    system_temp = Path(tempfile.gettempdir()).resolve()
    try:
        path.relative_to(system_temp)
    except ValueError:
        return
    clean_detail = str(detail).replace("\r", " ").replace("\n", " ")[:500]
    payload = f"{os.getpid()}\t{clean_detail}\n".encode("utf-8")
    descriptor = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        os.write(descriptor, payload)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def isolated_backend_test_network_guard(
    *,
    fatal_exit_code: int | None = None,
) -> Iterator[BackendTestNetworkAudit]:
    """Allow ephemeral loopback tests while denying real services and the internet.

    The configured Futu sentinel at ``127.0.0.1:1`` is answered in-process as
    connection-refused, so even the expected offline probe never reaches the OS.
    """

    audit = BackendTestNetworkAudit()
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_send = socket.socket.send
    original_sendall = socket.socket.sendall
    original_sendto = socket.socket.sendto
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo
    original_gethostbyname = socket.gethostbyname
    original_gethostbyname_ex = socket.gethostbyname_ex

    def guarded_connect(sock: socket.socket, address: Any) -> Any:
        if getattr(socket, "AF_UNIX", None) == sock.family:
            return original_connect(sock, address)
        _host, port = _validate_network_destination(
            address,
            operation="connect",
            audit=audit,
            fatal_exit_code=fatal_exit_code,
        )
        if port == _OFFLINE_SENTINEL_PORT:
            audit.record_simulated_offline()
            raise ConnectionRefusedError(
                errno.ECONNREFUSED,
                "isolated offline sentinel",
            )
        audit.record_allowed()
        return original_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: Any) -> int:
        if getattr(socket, "AF_UNIX", None) == sock.family:
            return original_connect_ex(sock, address)
        _host, port = _validate_network_destination(
            address,
            operation="connect_ex",
            audit=audit,
            fatal_exit_code=fatal_exit_code,
        )
        if port == _OFFLINE_SENTINEL_PORT:
            audit.record_simulated_offline()
            return errno.ECONNREFUSED
        audit.record_allowed()
        return original_connect_ex(sock, address)

    def guarded_sendto(sock: socket.socket, data: Any, *args: Any) -> int:
        if getattr(socket, "AF_UNIX", None) == sock.family:
            return original_sendto(sock, data, *args)
        address = args[-1] if args else None
        _host, port = _validate_network_destination(
            address,
            operation="sendto",
            audit=audit,
            fatal_exit_code=fatal_exit_code,
        )
        if port == _OFFLINE_SENTINEL_PORT:
            audit.record_simulated_offline()
            raise ConnectionRefusedError(
                errno.ECONNREFUSED,
                "isolated offline sentinel",
            )
        audit.record_allowed()
        return original_sendto(sock, data, *args)

    def guarded_send(sock: socket.socket, data: Any, *args: Any) -> int:
        if getattr(socket, "AF_UNIX", None) == sock.family:
            return original_send(sock, data, *args)
        try:
            peer = sock.getpeername()
        except OSError:
            detail = "send:unconnected_inet_socket"
            _deny_network_access(
                audit,
                detail,
                "isolated backend tests rejected an unconnected INET send",
                fatal_exit_code=fatal_exit_code,
            )
        _validate_network_destination(
            peer,
            operation="send",
            audit=audit,
            fatal_exit_code=fatal_exit_code,
        )
        return original_send(sock, data, *args)

    def guarded_sendall(sock: socket.socket, data: Any, *args: Any) -> None:
        if getattr(socket, "AF_UNIX", None) == sock.family:
            return original_sendall(sock, data, *args)
        try:
            peer = sock.getpeername()
        except OSError:
            detail = "sendall:unconnected_inet_socket"
            _deny_network_access(
                audit,
                detail,
                "isolated backend tests rejected an unconnected INET sendall",
                fatal_exit_code=fatal_exit_code,
            )
        _validate_network_destination(
            peer,
            operation="sendall",
            audit=audit,
            fatal_exit_code=fatal_exit_code,
        )
        return original_sendall(sock, data, *args)

    def guarded_create_connection(
        address: Any,
        timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
        source_address: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> socket.socket:
        _host, port = _validate_network_destination(
            address,
            operation="create_connection",
            audit=audit,
            fatal_exit_code=fatal_exit_code,
        )
        if port == _OFFLINE_SENTINEL_PORT:
            audit.record_simulated_offline()
            raise ConnectionRefusedError(
                errno.ECONNREFUSED,
                "isolated offline sentinel",
            )
        return original_create_connection(
            address,
            timeout=timeout,
            source_address=source_address,
            *args,
            **kwargs,
        )

    def guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _is_loopback_host(host):
            detail = f"getaddrinfo:{_canonical_network_host(host) or '<blank>'}:{port}"
            _deny_network_access(
                audit,
                detail,
                f"isolated backend tests forbid external DNS resolution: {detail}",
                fatal_exit_code=fatal_exit_code,
            )
        return original_getaddrinfo(host, port, *args, **kwargs)

    def guarded_gethostbyname(host: Any) -> str:
        if not _is_loopback_host(host):
            detail = f"gethostbyname:{_canonical_network_host(host) or '<blank>'}"
            _deny_network_access(
                audit,
                detail,
                f"isolated backend tests forbid external DNS resolution: {detail}",
                fatal_exit_code=fatal_exit_code,
            )
        return original_gethostbyname(host)

    def guarded_gethostbyname_ex(host: Any) -> Any:
        if not _is_loopback_host(host):
            detail = f"gethostbyname_ex:{_canonical_network_host(host) or '<blank>'}"
            _deny_network_access(
                audit,
                detail,
                f"isolated backend tests forbid external DNS resolution: {detail}",
                fatal_exit_code=fatal_exit_code,
            )
        return original_gethostbyname_ex(host)

    with ExitStack() as stack:
        stack.enter_context(patch.object(socket.socket, "connect", guarded_connect))
        stack.enter_context(patch.object(socket.socket, "connect_ex", guarded_connect_ex))
        stack.enter_context(patch.object(socket.socket, "send", guarded_send))
        stack.enter_context(patch.object(socket.socket, "sendall", guarded_sendall))
        stack.enter_context(patch.object(socket.socket, "sendto", guarded_sendto))
        stack.enter_context(patch.object(socket, "create_connection", guarded_create_connection))
        stack.enter_context(patch.object(socket, "getaddrinfo", guarded_getaddrinfo))
        stack.enter_context(patch.object(socket, "gethostbyname", guarded_gethostbyname))
        stack.enter_context(patch.object(socket, "gethostbyname_ex", guarded_gethostbyname_ex))
        yield audit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run backend tests with a system-temp runtime and SQLite"
    )
    parser.add_argument("tests", nargs="*", help="Optional unittest dotted names")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--layer",
        choices=BACKEND_TEST_LAYER_IDS,
        help="Run one canonical backend test layer",
    )
    selection.add_argument(
        "--list-layers",
        action="store_true",
        help="Validate and list the canonical backend test layers without running tests",
    )
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--start-directory", default="tests")
    parser.add_argument("--verbosity", type=int, default=1)
    parser.add_argument(
        "--durations",
        type=int,
        default=20,
        help="Report the N slowest tests (0 reports every test duration)",
    )
    return parser


def _safe_relative_directory(project_root: Path, value: Any) -> str:
    clean = str(value or "").strip().replace("\\", "/")
    if not clean or clean.startswith("/") or ":" in clean:
        raise BackendTestLayerError(
            "a discover layer start_directory must be a relative directory"
        )
    relative = Path(clean)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise BackendTestLayerError(
            "a discover layer start_directory must be canonical"
        )
    resolved = (project_root / relative).resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise BackendTestLayerError(
            "a discover layer start_directory leaves the project"
        ) from exc
    if not resolved.is_dir():
        raise BackendTestLayerError(
            f"discover layer directory does not exist: {clean}"
        )
    return relative.as_posix()


def _validate_test_module(project_root: Path, value: Any) -> str:
    if not isinstance(value, str):
        raise BackendTestLayerError("layer tests must be dotted module strings")
    clean = value.strip()
    if clean != value or not _TEST_MODULE_PATTERN.fullmatch(clean):
        raise BackendTestLayerError(
            f"layer test must be a canonical tests.test_* module: {value!r}"
        )
    module_path = project_root.joinpath(*clean.split(".")).with_suffix(".py")
    try:
        module_path.resolve().relative_to(project_root)
    except ValueError as exc:  # pragma: no cover - guarded by the dotted pattern
        raise BackendTestLayerError("layer test module leaves the project") from exc
    if not module_path.is_file():
        raise BackendTestLayerError(f"layer test module does not exist: {clean}")
    return clean


def load_backend_test_layer_manifest(
    manifest_path: str | Path | None = None,
    *,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Load and strictly validate the closed, versioned layer manifest.

    Validation is filesystem-only.  Test modules are never imported before the
    isolated runtime and database environment has been installed.
    """

    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
    path = Path(manifest_path or BACKEND_TEST_LAYER_MANIFEST_PATH).resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackendTestLayerError("backend test layer manifest is unreadable") from exc
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
        raise BackendTestLayerError("backend test layer manifest is not closed")
    if value.get("version") != BACKEND_TEST_LAYER_MANIFEST_VERSION:
        raise BackendTestLayerError("backend test layer manifest version is unsupported")
    raw_layers = value.get("layers")
    if not isinstance(raw_layers, list) or len(raw_layers) != len(
        BACKEND_TEST_LAYER_IDS
    ):
        raise BackendTestLayerError("backend test layer set is incomplete")

    normalized_layers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_tests: set[str] = set()
    for index, raw_layer in enumerate(raw_layers):
        if not isinstance(raw_layer, dict) or set(raw_layer) != _LAYER_KEYS:
            raise BackendTestLayerError("a backend test layer is not closed")
        layer_id = str(raw_layer.get("id") or "").strip()
        if layer_id != BACKEND_TEST_LAYER_IDS[index] or layer_id in seen_ids:
            raise BackendTestLayerError(
                "backend test layers must use the canonical unique order"
            )
        seen_ids.add(layer_id)
        description = raw_layer.get("description")
        if (
            not isinstance(description, str)
            or description != description.strip()
            or not description
            or len(description) > 240
        ):
            raise BackendTestLayerError("a backend test layer description is invalid")
        selection = raw_layer.get("selection")
        tests = raw_layer.get("tests")
        start_directory = raw_layer.get("start_directory")
        pattern = raw_layer.get("pattern")
        if selection == "modules":
            if (
                layer_id == "full"
                or not isinstance(tests, list)
                or not tests
                or start_directory != ""
                or pattern != ""
            ):
                raise BackendTestLayerError("a module layer has an invalid selection")
            normalized_tests: list[str] = []
            for raw_test in tests:
                test_module = _validate_test_module(root, raw_test)
                if test_module in seen_tests:
                    raise BackendTestLayerError(
                        f"test module is assigned more than once: {test_module}"
                    )
                seen_tests.add(test_module)
                normalized_tests.append(test_module)
            if normalized_tests != sorted(normalized_tests):
                raise BackendTestLayerError(
                    f"layer tests must be sorted canonically: {layer_id}"
                )
            normalized_start_directory = ""
            normalized_pattern = ""
        elif selection == "discover":
            if layer_id != "full" or tests != [] or pattern != "test_*.py":
                raise BackendTestLayerError("the full discover layer is invalid")
            normalized_tests = []
            normalized_start_directory = _safe_relative_directory(
                root, start_directory
            )
            normalized_pattern = pattern
        else:
            raise BackendTestLayerError("a backend test layer selection is invalid")
        normalized_layers.append({
            "id": layer_id,
            "description": description,
            "selection": selection,
            "tests": normalized_tests,
            "start_directory": normalized_start_directory,
            "pattern": normalized_pattern,
        })
    return {
        "version": BACKEND_TEST_LAYER_MANIFEST_VERSION,
        "layers": normalized_layers,
    }


def resolve_backend_test_selection(
    args: argparse.Namespace,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Resolve CLI arguments without importing or executing a test module."""

    explicit_tests = list(getattr(args, "tests", None) or [])
    layer_id = str(getattr(args, "layer", None) or "")
    list_layers = bool(getattr(args, "list_layers", False))
    if layer_id and explicit_tests:
        raise BackendTestLayerError(
            "--layer cannot be combined with explicit dotted tests"
        )
    if list_layers and explicit_tests:
        raise BackendTestLayerError(
            "--list-layers cannot be combined with explicit dotted tests"
        )
    if (layer_id or list_layers) and (
        str(getattr(args, "start_directory", "tests")) != "tests"
        or str(getattr(args, "pattern", "test_*.py")) != "test_*.py"
    ):
        raise BackendTestLayerError(
            "layer selection cannot be combined with discovery overrides"
        )
    if list_layers:
        return {"selection": "list", "tests": [], "start_directory": "", "pattern": ""}
    if layer_id:
        layer = next(
            (
                row
                for row in manifest.get("layers") or []
                if row.get("id") == layer_id
            ),
            None,
        )
        if not isinstance(layer, dict):
            raise BackendTestLayerError(f"unknown backend test layer: {layer_id}")
        return {
            "selection": str(layer["selection"]),
            "tests": list(layer["tests"]),
            "start_directory": str(layer["start_directory"]),
            "pattern": str(layer["pattern"]),
        }
    if explicit_tests:
        return {
            "selection": "modules",
            "tests": explicit_tests,
            "start_directory": "",
            "pattern": "",
        }
    return {
        "selection": "discover",
        "tests": [],
        "start_directory": str(getattr(args, "start_directory", "tests")),
        "pattern": str(getattr(args, "pattern", "test_*.py")),
    }


def configure_isolated_test_environment(runtime_path: str | Path) -> Path:
    runtime = Path(runtime_path).resolve()
    if not runtime.is_dir():
        raise BackendTestLayerError("isolated test runtime must already exist")
    system_temp = Path(tempfile.gettempdir()).resolve()
    try:
        relative_runtime = runtime.relative_to(system_temp)
    except ValueError as exc:
        raise BackendTestLayerError(
            "isolated test runtime must be inside the system temp directory"
        ) from exc
    if not relative_runtime.parts:
        raise BackendTestLayerError(
            "isolated test runtime must be a dedicated system-temp directory"
        )
    os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    os.environ["AI_STUDIO_RUNTIME_DIR"] = str(runtime)
    os.environ["AI_STUDIO_DATABASE_PATH"] = str(
        runtime / "unittest-default.sqlite3"
    )
    os.environ["FUTU_HOST"] = "127.0.0.1"
    os.environ["FUTU_PORT"] = "1"
    for name in _PROVIDER_KEY_NAMES:
        os.environ.pop(name, None)
    for name in _PROXY_KEY_NAMES:
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    os.environ["AI_STUDIO_TEST_NETWORK_GUARD"] = "1"
    child_audit_path = runtime / _CHILD_NETWORK_AUDIT_FILE_NAME
    if child_audit_path.exists():
        raise BackendTestLayerError(
            "isolated child-process network audit path already exists"
        )
    os.environ[_CHILD_NETWORK_AUDIT_ENV] = str(child_audit_path)
    bootstrap = _CHILD_BOOTSTRAP_DIR.resolve()
    project_root = Path(__file__).resolve().parents[1]
    if not (bootstrap / "sitecustomize.py").is_file():
        raise BackendTestLayerError(
            "isolated child-process network bootstrap is missing"
        )
    os.environ["PYTHONPATH"] = os.pathsep.join(
        (str(bootstrap), str(project_root))
    )
    os.environ["PYTHONSAFEPATH"] = "1"
    return runtime


def read_child_network_blocks(runtime_path: str | Path) -> list[str]:
    runtime = Path(runtime_path).resolve()
    path = runtime / _CHILD_NETWORK_AUDIT_FILE_NAME
    if not path.exists():
        return []
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise BackendTestLayerError(
            "isolated child-process network audit is unreadable"
        ) from exc
    return [row[:600] for row in rows if row.strip()]


def verify_isolated_child_network_bootstrap() -> None:
    """Prove a fresh Python child installed this exact-run socket bootstrap."""

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "expected=str(os.getpid()); "
                "actual=os.environ.get('AI_STUDIO_TEST_NETWORK_CHILD_GUARD_ACTIVE',''); "
                "raise SystemExit(0 if actual==expected else 87)"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "no child diagnostics").strip()
        raise BackendTestLayerError(
            "isolated child-process network bootstrap failed: " + detail[:500]
        )


def build_backend_test_suite(
    loader: unittest.TestLoader,
    selection: dict[str, Any],
) -> unittest.TestSuite:
    if selection["selection"] == "modules":
        return loader.loadTestsFromNames(selection["tests"])
    if selection["selection"] == "discover":
        return loader.discover(
            start_dir=selection["start_directory"],
            pattern=selection["pattern"],
            top_level_dir=".",
        )
    raise BackendTestLayerError("a runnable backend test selection is required")


def _print_layers(manifest: dict[str, Any]) -> None:
    print(f"manifest_version={manifest['version']}")
    for layer in manifest["layers"]:
        count = (
            len(layer["tests"])
            if layer["selection"] == "modules"
            else "discover"
        )
        print(
            f"{layer['id']}\t{layer['selection']}\t{count}\t"
            f"{layer['description']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.durations < 0:
        parser.error("--durations must be zero or a positive integer")
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    os.chdir(project_root)
    try:
        manifest = load_backend_test_layer_manifest(project_root=project_root)
        selection = resolve_backend_test_selection(args, manifest)
    except BackendTestLayerError as exc:
        parser.error(str(exc))
    if selection["selection"] == "list":
        _print_layers(manifest)
        return 0

    with tempfile.TemporaryDirectory(
        prefix="ai-collaboration-studio-tests-"
    ) as temp_dir:
        runtime_path = configure_isolated_test_environment(temp_dir)
        verify_isolated_child_network_bootstrap()
        with isolated_backend_test_network_guard() as network_audit:
            suite = build_backend_test_suite(unittest.defaultTestLoader, selection)
            print(f"isolated_runtime={runtime_path}", flush=True)
            result = unittest.TextTestRunner(
                verbosity=args.verbosity,
                durations=args.durations,
            ).run(suite)
        network_report = network_audit.report()
        child_blocks = read_child_network_blocks(runtime_path)
        network_report["child_blocked_attempt_count"] = len(child_blocks)
        network_report["child_blocked_attempts"] = child_blocks[:20]
        print(
            "network_isolation="
            + json.dumps(network_report, ensure_ascii=False, sort_keys=True),
            flush=True,
        )
        return 0 if (
            result.wasSuccessful()
            and network_report["blocked_attempt_count"] == 0
            and network_report["child_blocked_attempt_count"] == 0
        ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
