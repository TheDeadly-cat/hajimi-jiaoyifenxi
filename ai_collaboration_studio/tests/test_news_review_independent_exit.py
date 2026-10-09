"""X-05 offline checks: only newly spawned disposable processes, no product imports."""
from __future__ import annotations

import hashlib
import ast
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "record_news_review_exit.py"
spec = importlib.util.spec_from_file_location("independent_exit_under_test", SCRIPT)
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)
TICKS = 134000000000000001


def bound_runtime_pin(pid):
    # Execute only the frozen watchdog's pure native identity function. Do not
    # import its module, initialize services, or access any product database.
    source = SCRIPT.with_name("news_review_watchdog.py")
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "native_creation_ticks")
    namespace = {"os": os, "ctypes": ctypes}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    value = namespace["native_creation_ticks"](pid)
    if type(value) is not int:
        raise AssertionError("frozen runtime native pin unavailable")
    return {"pid": pid, "process_start_utc_ticks": str(value)}


def request(pins, duration=4000):
    return {"version": "news_review_exit_request_v1", "run_id": uuid.uuid4().hex,
            "identity": {"candidate_sha": "b625221d4e517e496a1f2fe0d389ad4decc5d1ca",
                         "policy_sha256": None, "activation_sha256": None,
                         "observer_plan_sha256": None},
            "maximum_observation_ms": duration, "targets": pins}


class FakeNative:
    def __init__(self, *, mismatch=False, wait_error=False, code_error=False):
        self.mismatch, self.wait_error, self.code_error = mismatch, wait_error, code_error
        self.closed = []

    def pin(self, pid):
        return {"pid": pid, "process_start_utc_ticks": str(TICKS - 1)}

    def open(self, pid):
        return pid

    def times(self, handle):
        return TICKS + int(self.mismatch), TICKS + 100, 0, 0

    def wait(self, handle):
        if self.wait_error:
            raise RuntimeError("PRIVATE_EXCEPTION_MESSAGE_MUST_NOT_APPEAR")
        return recorder.WAIT_OBJECT_0

    def exit_code(self, handle):
        if self.code_error:
            raise recorder.RecorderFault("exit_code_unconfirmed", native_error=5)
        return 259

    def close(self, handle):
        self.closed.append(handle)


class LogicAndPersistenceTests(unittest.TestCase):
    def run_case(self, api, sink):
        pin = {"role": "host", "pid": os.getpid() + 1000,
               "process_start_utc_ticks": str(TICKS)}
        return recorder.observe(recorder.validate_request(request([pin])), "a" * 64, sink, api)

    def test_full_precision_real_exit_259_and_no_self_certifying_final(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = recorder.EvidenceDirectory(Path(directory) / "evidence")
            api = FakeNative()
            value, code = self.run_case(api, sink)
            self.assertEqual(0, code)
            self.assertEqual(259, value["targets"][0]["exit_code"])
            self.assertEqual(str(TICKS), value["targets"][0]["process_start_utc_ticks"])
            self.assertEqual([os.getpid() + 1000], api.closed)
            final = json.loads((sink.path / "recorder-final.json").read_text())
            self.assertTrue(final["observations_complete"])
            self.assertFalse(final["recording_complete"])
            self.assertIsNone(final["summary_persisted"])
            self.assertTrue(value["recording_complete"])
            self.assertTrue(value["summary_persisted"])
            self.assertFalse(value["monitoring_acceptance"])
            self.assertFalse(value["ledger_drained_verified"])

    def test_identity_mismatch_never_claims_an_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeNative(mismatch=True)
            value, code = self.run_case(api, recorder.EvidenceDirectory(Path(directory) / "e"))
            self.assertEqual(2, code)
            target = value["targets"][0]
            self.assertEqual("UNCONFIRMED", target["exit_status"])
            self.assertIsNone(target["exit_code"])
            self.assertEqual("process_identity_mismatch", target["failure"]["code"])
            self.assertEqual([os.getpid() + 1000], api.closed)

    def test_wait_error_is_bounded_and_handle_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeNative(wait_error=True)
            value, code = self.run_case(api, recorder.EvidenceDirectory(Path(directory) / "e"))
            self.assertEqual(2, code)
            self.assertEqual("UNCONFIRMED", value["targets"][0]["exit_status"])
            self.assertNotIn("PRIVATE_EXCEPTION_MESSAGE", json.dumps(value))
            self.assertEqual([os.getpid() + 1000], api.closed)

    def test_signaled_exit_preserves_fact_when_exit_code_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeNative(code_error=True)
            value, code = self.run_case(api, recorder.EvidenceDirectory(Path(directory) / "e"))
            self.assertEqual(2, code)
            self.assertEqual("CONFIRMED", value["targets"][0]["exit_status"])
            self.assertIsNone(value["targets"][0]["exit_code"])
            self.assertEqual("UNCONFIRMED", value["targets"][0]["exit_code_status"])

    def test_exit_write_and_final_write_failure_cannot_return_success(self):
        class FailingSink:
            def publish(self, name, value):
                if name.endswith("-exit.json") or name == "recorder-final.json":
                    raise OSError("PRIVATE_PATH_MUST_NOT_APPEAR")
        api = FakeNative()
        value, code = self.run_case(api, FailingSink())
        self.assertEqual(2, code)
        self.assertFalse(value["recording_complete"])
        self.assertFalse(value["summary_persisted"])
        self.assertNotIn("PRIVATE_PATH", json.dumps(value))
        self.assertEqual([os.getpid() + 1000], api.closed)

    def test_fresh_directory_and_file_never_overwrite_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "e"
            sink = recorder.EvidenceDirectory(path)
            sink.publish("one.json", {"first": 1})
            frozen = (path / "one.json").read_bytes()
            with self.assertRaises(FileExistsError):
                sink.publish("one.json", {"second": 2})
            self.assertEqual(frozen, (path / "one.json").read_bytes())
            with self.assertRaises(FileExistsError):
                recorder.EvidenceDirectory(path)

    def test_rounded_ticks_duplicate_json_and_bad_hash_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_bytes(b'{"version":1,"version":2}')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(recorder.RecorderFault):
                recorder.read_request(path, digest)
            with self.assertRaises(recorder.RecorderFault):
                recorder.read_request(path, "0" * 64)
            with self.assertRaises(recorder.RecorderFault):
                recorder.ticks(float(TICKS))


@unittest.skipUnless(os.name == "nt", "Windows native process handles required")
class NativeCliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="x05-owned-fixtures-")
        self.directory = Path(self.temp.name)
        self.children = []
        self.api = recorder.WindowsProcesses()
        self.recorder_process = None

    def tearDown(self):
        for process in [self.recorder_process, *self.children]:
            if process is None:
                continue
            if process.poll() is None:
                process.kill()  # Only this test's own Popen handle.
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
        self.temp.cleanup()

    def child(self, code):
        body = "import sys, os\nsys.stdin.buffer.read(1)\nos._exit(" + str(code) + ")\n"
        process = subprocess.Popen([sys.executable, "-I", "-S", "-c", body],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        self.children.append(process)
        return process

    def launch(self, pins, duration=5000):
        plan = self.directory / "request.json"
        plan.write_bytes((json.dumps(request(pins, duration), sort_keys=True) + "\n").encode())
        digest = hashlib.sha256(plan.read_bytes()).hexdigest()
        out = self.directory / "evidence"
        self.recorder_process = subprocess.Popen(
            [sys.executable, "-I", "-S", str(SCRIPT), "--request", str(plan),
             "--request-sha256", digest, "--output", str(out)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        return out, digest

    def wait_receipt(self, out, name):
        path = out / name
        deadline = time.monotonic() + 6
        while not path.exists() and time.monotonic() < deadline:
            if self.recorder_process.poll() is not None:
                stdout, stderr = self.recorder_process.communicate(timeout=3)
                self.fail("recorder ended before binding: " + stdout.decode() + stderr.decode())
            time.sleep(0.01)
        self.assertTrue(path.is_file(), name)
        return json.loads(path.read_text())

    def finished(self):
        stdout, stderr = self.recorder_process.communicate(timeout=10)
        self.assertEqual(b"", stderr)
        return json.loads(stdout), self.recorder_process.returncode

    def test_two_native_targets_normal_exit_and_real_259(self):
        first, second = self.child(0), self.child(259)
        pins = [{**bound_runtime_pin(first.pid), "role": "host"},
                {**bound_runtime_pin(second.pid), "role": "observer"}]
        self.assertEqual(pins[0]["process_start_utc_ticks"],
                         self.api.pin(first.pid)["process_start_utc_ticks"])
        out, digest = self.launch(pins)
        for role in ("host", "observer"):
            self.wait_receipt(out, f"target-{role}-bound.json")
        for process in (first, second):
            process.stdin.write(b"x"); process.stdin.flush()
        value, code = self.finished()
        self.assertEqual(0, code)
        self.assertTrue(value["recording_complete"])
        self.assertEqual(digest, value["request_sha256"])
        self.assertEqual([0, 259], [t["exit_code"] for t in value["targets"]])
        for target in value["targets"]:
            self.assertEqual("CONFIRMED", target["exit_status"])
            self.assertGreater(int(target["process_start_utc_ticks"]), 2**53)
            self.assertGreaterEqual(int(target["native_exit_utc_ticks"]),
                                    int(target["process_start_utc_ticks"]))
        final = json.loads((out / "recorder-final.json").read_text())
        self.assertTrue(final["observations_complete"])
        self.assertIsNone(final["summary_persisted"])
        self.assertFalse(final["recording_complete"])

    def test_forced_owned_target_exit_still_retains_native_code(self):
        process = self.child(0)
        out, _ = self.launch([{**self.api.pin(process.pid), "role": "host"}])
        self.wait_receipt(out, "target-host-bound.json")
        process.kill()
        value, code = self.finished()
        self.assertEqual(0, code)
        self.assertEqual("CONFIRMED", value["targets"][0]["exit_status"])
        self.assertIsInstance(value["targets"][0]["exit_code"], int)
        self.assertNotEqual(0, value["targets"][0]["exit_code"])

    def test_native_wrong_generation_unconfirmed_without_killing_target(self):
        process = self.child(0)
        pin = self.api.pin(process.pid)
        pin["process_start_utc_ticks"] = str(int(pin["process_start_utc_ticks"]) + 1)
        out, _ = self.launch([{**pin, "role": "host"}])
        value, code = self.finished()
        self.assertEqual(2, code)
        self.assertEqual("UNCONFIRMED", value["targets"][0]["exit_status"])
        self.assertIsNone(value["targets"][0]["exit_code"])
        self.assertIsNone(process.poll())

    def test_native_observation_limit_does_not_imply_death(self):
        process = self.child(0)
        out, _ = self.launch([{**self.api.pin(process.pid), "role": "host"}], duration=150)
        value, code = self.finished()
        self.assertEqual(2, code)
        self.assertEqual("observation_limit_elapsed", value["targets"][0]["failure"]["code"])
        self.assertEqual("UNCONFIRMED", value["targets"][0]["exit_status"])
        self.assertIsNone(process.poll())

    def test_owned_recorder_killed_keeps_pending_evidence_not_success(self):
        process = self.child(0)
        out, _ = self.launch([{**self.api.pin(process.pid), "role": "host"}])
        self.wait_receipt(out, "target-host-bound.json")
        self.recorder_process.kill()
        self.recorder_process.communicate(timeout=5)
        self.assertFalse((out / "recorder-final.json").exists())
        start = json.loads((out / "recorder-start.json").read_text())
        self.assertFalse(start["recording_complete"])
        self.assertEqual("UNCONFIRMED", start["targets"][0]["exit_status"])
        self.assertIsNone(process.poll())


if __name__ == "__main__":
    unittest.main()
