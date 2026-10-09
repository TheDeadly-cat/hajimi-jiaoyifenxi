"""Independent Windows process-exit evidence; no product imports or product I/O."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

VERSION = "news_review_independent_exit_v1"
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 258
QUERY_AND_SYNCHRONIZE = 0x1000 | 0x00100000
MAX_REQUEST_BYTES = 65536
# Match the existing host/observer pins: 100 ns UTC ticks since 0001-01-01.
# Windows FILETIME uses 1601-01-01. CPU duration fields have no epoch offset.
FILETIME_TO_UTC_TICKS = 504911232000000000


class RecorderFault(Exception):
    def __init__(self, code, *, native_error=None):
        self.code, self.native_error = code, native_error
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise RecorderFault(code)


def bounded_failure(exc, code):
    # Never retain exception messages, command lines, environment or credentials.
    return {"code": getattr(exc, "code", code),
            "exception_type": type(exc).__name__[:80],
            "native_error": getattr(exc, "native_error", None)}


def ticks(value):
    require(type(value) in (int, str), "creation_ticks_invalid")
    text = str(value)
    require(re.fullmatch(r"[1-9][0-9]{0,19}", text) is not None
            and int(text) <= 2**64 - 1, "creation_ticks_invalid")
    return text


def validate_request(value):
    require(type(value) is dict and set(value) == {
        "version", "run_id", "identity", "targets", "maximum_observation_ms"},
        "request_fields_invalid")
    require(value["version"] == "news_review_exit_request_v1", "request_version_invalid")
    require(type(value["run_id"]) is str
            and re.fullmatch(r"[0-9a-f]{32}", value["run_id"]), "run_id_invalid")
    identity = value["identity"]
    require(type(identity) is dict and set(identity) == {
        "candidate_sha", "policy_sha256", "activation_sha256", "observer_plan_sha256"},
        "run_identity_invalid")
    require(type(identity["candidate_sha"]) is str
            and re.fullmatch(r"[0-9a-f]{40}", identity["candidate_sha"]), "candidate_invalid")
    for key in ("policy_sha256", "activation_sha256", "observer_plan_sha256"):
        require(identity[key] is None or (type(identity[key]) is str
                and re.fullmatch(r"[0-9a-f]{64}", identity[key])), "identity_digest_invalid")
    duration = value["maximum_observation_ms"]
    require(type(duration) is int and 1 <= duration <= 172800000, "observation_limit_invalid")
    targets = value["targets"]
    require(type(targets) is list and 1 <= len(targets) <= 2, "targets_invalid")
    roles, pids = set(), set()
    normalized = []
    for target in targets:
        require(type(target) is dict and set(target) == {
            "role", "pid", "process_start_utc_ticks"}, "target_fields_invalid")
        role, pid = target["role"], target["pid"]
        require(role in ("host", "observer") and role not in roles, "target_role_invalid")
        require(type(pid) is int and 0 < pid <= 0xFFFFFFFF and pid not in pids,
                "target_pid_invalid")
        roles.add(role); pids.add(pid)
        normalized.append({**target, "process_start_utc_ticks": ticks(target["process_start_utc_ticks"])})
    return {**value, "targets": normalized}


def no_reparse(path):
    require(path.is_absolute(), "absolute_path_required")
    require(not str(path).startswith(("\\\\", "//")), "local_path_required")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode)
                and not (getattr(info, "st_file_attributes", 0) & 0x400),
                "reparse_path_rejected")


def read_request(path, expected_digest):
    require(type(expected_digest) is str
            and re.fullmatch(r"[0-9a-f]{64}", expected_digest), "request_digest_invalid")
    path = Path(path)
    no_reparse(path)
    require(path.suffix.lower() == ".json" and path.is_file()
            and path.stat().st_size <= MAX_REQUEST_BYTES, "request_file_invalid")
    with path.open("rb") as stream:
        raw = stream.read(MAX_REQUEST_BYTES + 1)
    require(len(raw) <= MAX_REQUEST_BYTES, "request_too_large")
    require(hashlib.sha256(raw).hexdigest() == expected_digest, "request_digest_mismatch")

    def unique_pairs(pairs):
        result = {}
        for key, item in pairs:
            require(key not in result, "duplicate_json_key")
            result[key] = item
        return result

    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(RecorderFault("json_constant_invalid")))
    return validate_request(value)


class WindowsProcesses:
    """Only query and synchronization rights; never terminate or enumerate processes."""
    def __init__(self):
        require(os.name == "nt", "native_windows_required")
        self.dll = ctypes.WinDLL("kernel32", use_last_error=True)
        self.dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.dll.OpenProcess.restype = wintypes.HANDLE
        self.dll.CloseHandle.argtypes = [wintypes.HANDLE]
        self.dll.CloseHandle.restype = wintypes.BOOL
        self.dll.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.dll.GetProcessTimes.restype = wintypes.BOOL
        self.dll.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.dll.WaitForSingleObject.restype = wintypes.DWORD
        self.dll.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.dll.GetExitCodeProcess.restype = wintypes.BOOL

    def open(self, pid):
        handle = self.dll.OpenProcess(QUERY_AND_SYNCHRONIZE, False, pid)
        if not handle:
            raise RecorderFault("open_process_unconfirmed", native_error=ctypes.get_last_error())
        return handle

    def times(self, handle):
        values = [wintypes.FILETIME() for _ in range(4)]
        if not self.dll.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            raise RecorderFault("process_times_unconfirmed", native_error=ctypes.get_last_error())
        native = tuple((v.dwHighDateTime << 32) | v.dwLowDateTime for v in values)
        return (native[0] + FILETIME_TO_UTC_TICKS,
                native[1] + FILETIME_TO_UTC_TICKS if native[1] else 0,
                native[2], native[3])

    def wait(self, handle):
        result = self.dll.WaitForSingleObject(handle, 0)
        if result not in (WAIT_OBJECT_0, WAIT_TIMEOUT):
            raise RecorderFault("native_wait_unconfirmed", native_error=ctypes.get_last_error())
        return result

    def exit_code(self, handle):
        code = wintypes.DWORD()
        if not self.dll.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise RecorderFault("exit_code_unconfirmed", native_error=ctypes.get_last_error())
        return code.value

    def close(self, handle):
        if not self.dll.CloseHandle(handle):
            raise RecorderFault("native_handle_close_failed", native_error=ctypes.get_last_error())

    def pin(self, pid):
        handle = self.open(pid)
        try:
            return {"pid": pid, "process_start_utc_ticks": str(self.times(handle)[0])}
        finally:
            self.close(handle)


class EvidenceDirectory:
    """Fresh local directory; exclusive, fsynced, atomic, read-back-checked files."""
    def __init__(self, path):
        self.path = Path(path)
        no_reparse(self.path)
        require(self.path.parent.is_dir(), "output_parent_required")
        self.path.mkdir(exist_ok=False)
        self.identity = self.directory_identity()

    def directory_identity(self):
        no_reparse(self.path)
        info = self.path.stat()
        require(stat.S_ISDIR(info.st_mode), "evidence_directory_invalid")
        return info.st_dev, info.st_ino

    def publish(self, name, value):
        require(re.fullmatch(r"[a-z0-9-]+\.json", name), "receipt_name_invalid")
        require(self.directory_identity() == self.identity, "evidence_directory_changed")
        raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
               + "\n").encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=".", suffix=".pending", dir=self.path)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            require(self.directory_identity() == self.identity, "evidence_directory_changed")
            destination = self.path / name
            os.link(temporary, destination)  # Never replace existing evidence.
            require(self.directory_identity() == self.identity, "evidence_directory_changed")
            with destination.open("rb") as stream:
                require(stream.read() == raw, "receipt_readback_mismatch")
        finally:
            # Only remove our exact temporary file when the directory is still bound.
            if self.directory_identity() == self.identity:
                Path(temporary).unlink(missing_ok=True)


def observe(request, request_digest, sink, native, *, wall_ms=None, monotonic_ms=None, sleep=None):
    wall_ms = wall_ms or (lambda: time.time_ns() // 1000000)
    monotonic_ms = monotonic_ms or (lambda: time.monotonic_ns() // 1000000)
    sleep = sleep or time.sleep
    own = native.pin(os.getpid())
    source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    common = {"version": VERSION, "run_id": request["run_id"], "identity": request["identity"],
              "identity_basis": "caller_supplied_not_independently_verified",
              "native_tick_basis": "dotnet_utc_100ns_since_0001_v1",
              "request_sha256": request_digest, "recorder_pin": own,
              "tool_source_sha256": source_hash,
              "monitoring_acceptance": False, "host_closeout_verified": False,
              "ledger_drained_verified": False, "power_loss_durability_proven": False}
    results = [{**target, "identity_verified": False, "exit_status": "UNCONFIRMED",
                "exit_code": None, "exit_code_status": "UNCONFIRMED",
                "native_wait_signaled": None, "native_exit_utc_ticks": None,
                "native_exit_time_status": "UNCONFIRMED", "detected_at_ms": None,
                "failure": None} for target in request["targets"]]
    handles, failures = {}, []
    begin = monotonic_ms()
    try:
        sink.publish("recorder-start.json", {**common, "started_at_ms": wall_ms(),
                     "maximum_observation_ms": request["maximum_observation_ms"],
                     "recording_complete": False, "targets": results})
        for index, target in enumerate(results):
            handle = None
            try:
                require(target["pid"] != own["pid"], "self_target_rejected")
                handle = native.open(target["pid"])
                require(str(native.times(handle)[0]) == target["process_start_utc_ticks"],
                        "process_identity_mismatch")
                target["identity_verified"] = True
                handles[index] = handle
                handle = None
                sink.publish(f"target-{target['role']}-bound.json",
                             {**common, "bound_at_ms": wall_ms(), "target": dict(target)})
            except Exception as exc:
                if handle is not None:
                    native.close(handle)
                target["failure"] = bounded_failure(exc, "target_binding_failed")
                sink.publish(f"target-{target['role']}-unconfirmed.json",
                             {**common, "observed_at_ms": wall_ms(), "target": dict(target)})

        pending = set(handles)
        while pending:
            now = monotonic_ms()
            require(now >= begin, "recorder_monotonic_changed")
            if now - begin >= request["maximum_observation_ms"]:
                for index in pending:
                    results[index]["failure"] = {"code": "observation_limit_elapsed",
                                                 "exception_type": None, "native_error": None}
                break
            for index in list(pending):
                target, handle = results[index], handles[index]
                try:
                    signal = native.wait(handle)
                    if signal == WAIT_TIMEOUT:
                        continue
                    target["native_wait_signaled"] = True
                    target["exit_status"] = "CONFIRMED"
                    target["detected_at_ms"] = wall_ms()
                    # Read the exit code only after the held process object is signaled.
                    # A genuine exit code of 259 is therefore not treated as "still alive".
                    target["exit_code"] = native.exit_code(handle)
                    target["exit_code_status"] = "CONFIRMED"
                    creation, exited, *_ = native.times(handle)
                    require(str(creation) == target["process_start_utc_ticks"],
                            "held_process_identity_unconfirmed")
                    require(exited > 0, "native_exit_time_unconfirmed")
                    target["native_exit_utc_ticks"] = str(exited)
                    target["native_exit_time_status"] = "CONFIRMED"
                except Exception as exc:
                    target["failure"] = bounded_failure(exc, "target_observation_failed")
                    if target["failure"]["code"] == "held_process_identity_unconfirmed":
                        target["identity_verified"] = False
                        target["exit_status"] = "UNCONFIRMED"
                        target["exit_code"] = None
                        target["exit_code_status"] = "UNCONFIRMED"
                sink.publish(f"target-{target['role']}-exit.json",
                             {**common, "observed_at_ms": wall_ms(), "target": dict(target)})
                pending.remove(index)
            if pending:
                sleep(min(0.1, max(0.001, (request["maximum_observation_ms"] -
                                         (monotonic_ms() - begin)) / 1000)))
    except (Exception, KeyboardInterrupt) as exc:
        failures.append(bounded_failure(exc, "recorder_interrupted" if isinstance(exc, KeyboardInterrupt)
                                        else "recorder_failed"))
    finally:
        for handle in handles.values():
            try:
                native.close(handle)
            except Exception as exc:
                failures.append(bounded_failure(exc, "native_handle_close_failed"))

    complete = not failures and all(t["identity_verified"] and t["exit_status"] == "CONFIRMED"
                                   and t["exit_code_status"] == "CONFIRMED"
                                   and t["native_exit_time_status"] == "CONFIRMED"
                                   and t["failure"] is None for t in results)
    summary = {**common, "completed_at_ms": wall_ms(), "recording_complete": False,
               "observations_complete": complete,
               "targets": results, "recorder_failures": failures, "summary_persisted": None}
    try:
        # A receipt cannot certify its own future publication. The file retains
        # UNKNOWN; the caller must also capture this process's stdout/exit code.
        sink.publish("recorder-final.json", summary)
        summary["summary_persisted"] = True
        summary["recording_complete"] = complete
    except Exception as exc:
        summary["recording_complete"] = False
        summary["summary_persisted"] = False
        summary["recorder_failures"].append(bounded_failure(exc, "final_receipt_write_failed"))
    return summary, 0 if summary["recording_complete"] else 2


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise RecorderFault("arguments_invalid")


def main(argv=None):
    try:
        parser = SafeArgumentParser(description=__doc__)
        parser.add_argument("--request", required=True)
        parser.add_argument("--request-sha256", required=True)
        parser.add_argument("--output", required=True)
        args = parser.parse_args(argv)
        request = read_request(args.request, args.request_sha256)
        native = WindowsProcesses()
        sink = EvidenceDirectory(args.output)
        summary, code = observe(request, args.request_sha256, sink, native)
    except (Exception, KeyboardInterrupt) as exc:
        summary = {"version": VERSION, "recording_complete": False, "summary_persisted": False,
                   "monitoring_acceptance": False, "failure": bounded_failure(exc, "recorder_start_failed")}
        code = 2
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
