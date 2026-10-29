"""Explicit robot/viewer clock correction, exercised over isolated real HTTP."""
import base64
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher
import live_launcher as live
from instance_identity import instance_identity
from server import Runtime, make_handler
from vla_evidence import VLAStore


class RobotClockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="r1-robot-clock-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot.json"
        self.db = self.root / "fleet.sqlite3"
        self.opener = build_opener(ProxyHandler({}))
        self.raw = self.frame(time.time())
        self.requests = []
        test = self

        class Camera(BaseHTTPRequestHandler):
            def do_GET(self):
                test.requests.append(self.path)
                body = test.raw
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.camera = self.serve(Camera)
        self.url = "http://127.0.0.1:%d/snapshot" % self.camera.server_port

    def frame(self, observed_at):
        image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
        return (json.dumps(dict(schema_version=1, robot_id="robot-clock", run_id="camera-run",
            observed_at=observed_at, task="clock test", model_id="no model", stage="camera",
            rtt_ms=123, images={key: image for key in ("head", "wrist_left", "wrist_right")})) + "\n").encode()

    def serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)

        self.addCleanup(close)
        return server

    def api(self, server, path):
        with self.opener.open("http://127.0.0.1:%d%s" % (server.server_port, path), timeout=3) as response:
            return json.load(response)

    def clock_call(self, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except TypeError as exc:
            if "robot_clock_offset" in str(exc):
                self.fail("Explicit robot_clock_offset contract is missing: " + str(exc))
            raise

    def handler(self, offset, store=None):
        runtime = Runtime(self.db, interval=10, fleet_size=4)
        self.addCleanup(runtime.close)
        return self.clock_call(make_handler, runtime, telemetry_file=self.snapshot,
                               vla_store=store, robot_clock_offset=offset)

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def run_script(self, name, *args):
        return subprocess.run([sys.executable, "-B", str(launcher.ROOT / name), *args],
            cwd=self.root, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True, timeout=8)

    def test_future_http_snapshot_needs_explicit_offset_and_preserves_bytes(self):
        self.raw = self.frame(time.time() + 13.5)
        with self.assertRaises(live.LiveError):
            live.fetch_snapshot(self.url)
        result = self.clock_call(live.fetch_snapshot, self.url, robot_clock_offset=13.5)
        self.assertEqual(result, self.raw)
        self.assertEqual(json.loads(result)["rtt_ms"], 123)

    def test_offset_cannot_refresh_stale_snapshot_and_supports_slow_robot(self):
        self.raw = self.frame(time.time() + 13.5 - 8)
        with self.assertRaises(live.LiveError):
            self.clock_call(live.fetch_snapshot, self.url, robot_clock_offset=13.5)
        self.raw = self.frame(time.time() - 13.5)
        self.assertEqual(self.clock_call(live.fetch_snapshot, self.url, robot_clock_offset=-13.5), self.raw)

    def test_api_corrects_age_and_vla_selection_without_changing_evidence(self):
        observed_at = time.time() + 13.5 - 1
        self.raw = self.frame(observed_at)
        self.snapshot.write_bytes(self.raw)
        store = VLAStore(self.root / "vla.sqlite3")
        self.addCleanup(store.close)
        event = dict(schema_version=1, event_id="real-event", robot_id="robot-clock",
            session_id="current-session", run_id="current-run", operation_id="run-op",
            kind="raw_response", observed_at=observed_at, request_id="request-4",
            payload=dict(attempt=4, chunk=2, rtt_s=.123, request_state23=[1] * 23,
                         response=dict(action23=[[2] * 23])))
        store.ingest([event], "source-clock", 100,
            health=dict(observed_at=observed_at, active_session_id="current-session", robot_id="robot-clock"))
        before = store.events("current-run")
        default_server = self.serve(self.handler(0, store))
        self.assertFalse(self.api(default_server, "/api/telemetry")["connected"])
        self.assertIsNone(self.api(default_server, "/api/vla/state")["state"]["run_id"])
        corrected_server = self.serve(self.handler(13.5, store))
        camera = self.api(corrected_server, "/api/telemetry")
        vla = self.api(corrected_server, "/api/vla/state")
        for envelope in (camera, vla):
            self.assertTrue(envelope["connected"], envelope)
            self.assertGreaterEqual(envelope["age_s"], .9)
            self.assertLess(envelope["age_s"], 3)
            self.assertEqual(envelope["robot_clock_offset_s"], 13.5)
        self.assertEqual(camera["state"]["observed_at"], observed_at)
        self.assertEqual(camera["state"]["rtt_ms"], 123)
        self.assertEqual(vla["state"]["request"]["id"], "request-4")
        self.assertEqual(vla["state"]["request"]["response_at"], observed_at)
        self.assertEqual(vla["state"]["request"]["rtt_ms"], 123)
        self.assertEqual(vla["state"]["request"]["action23"], [[2] * 23])
        self.assertEqual(self.snapshot.read_bytes(), self.raw)
        self.assertEqual(store.events("current-run"), before)

    def test_api_offset_preserves_stale_status(self):
        observed_at = time.time() + 13.5 - 20
        self.snapshot.write_bytes(self.frame(observed_at))
        store = VLAStore(self.root / "vla.sqlite3")
        self.addCleanup(store.close)
        store.ingest([dict(schema_version=1, event_id="old-event", robot_id="robot-clock",
            session_id="session", run_id="run", operation_id="op", kind="target",
            observed_at=observed_at, payload=dict(target23=[1] * 23))], "source", 100,
            health=dict(observed_at=observed_at))
        server = self.serve(self.handler(13.5, store))
        for path in ("/api/telemetry", "/api/vla/state"):
            envelope = self.api(server, path)
            self.assertFalse(envelope["connected"])
            self.assertEqual(envelope["status"], "stale")
            self.assertGreaterEqual(envelope["age_s"], 20)

    def test_zero_retains_legacy_digest_and_api_shape(self):
        self.assertEqual(self.clock_call(instance_identity, "/tmp/robot-clock-contract", ":memory:", 4,
            robot_clock_offset=0), "648f5e5b0c3ef0b09d277b10fb8fdda7c319ec191a10165a1af282e165a8f3cc")
        self.snapshot.write_bytes(self.frame(time.time()))
        server = self.serve(self.handler(0))
        self.assertTrue(self.api(server, "/api/telemetry")["connected"])
        for path in ("/api/telemetry", "/api/vla/state"):
            self.assertNotIn("robot_clock_offset_s", self.api(server, path))

    def test_server_launcher_passes_offset_and_refuses_wrong_offset_reuse(self):
        self.snapshot.write_bytes(self.frame(time.time() + 13.5 - 1))
        port = self.free_port()
        process = self.clock_call(launcher.ensure_server, port, self.db,
            telemetry_file=self.snapshot, robot_clock_offset=13.5)
        self.addCleanup(launcher.stop_owned_server, process)
        self.assertIsNotNone(process)
        with self.opener.open("http://127.0.0.1:%d/api/telemetry" % port, timeout=3) as response:
            envelope = json.load(response)
        self.assertTrue(envelope["connected"], envelope)
        self.assertEqual(envelope["robot_clock_offset_s"], 13.5)
        self.assertEqual(self.clock_call(launcher.probe, port, self.db,
            telemetry_file=self.snapshot, robot_clock_offset=13.5), "exhibit")
        self.assertEqual(launcher.probe(port, self.db, telemetry_file=self.snapshot), "incompatible")
        with self.assertRaises(launcher.LaunchError):
            self.clock_call(launcher.ensure_server, port, self.db,
                telemetry_file=self.snapshot, robot_clock_offset=12)
        self.assertIsNone(process.poll())
        self.assertIsNone(self.clock_call(launcher.ensure_server, port, self.db,
            telemetry_file=self.snapshot, robot_clock_offset=13.5))

    def test_check_cli_applies_offset_without_writing_snapshot_or_database(self):
        self.raw = self.frame(time.time() + 13.5)
        args = ["--robot-ip", "127.0.0.1", "--robot-port", str(self.camera.server_port),
            "--snapshot-file", str(self.snapshot), "--db", str(self.db), "--no-ssh", "--check"]
        default = self.run_script("live_launcher.py", *args)
        self.assertNotEqual(default.returncode, 0)
        adjusted = self.run_script("live_launcher.py", *args, "--robot-clock-offset", "13.5")
        self.assertEqual(adjusted.returncode, 0, adjusted.stdout + adjusted.stderr)
        self.assertFalse(self.snapshot.exists())
        self.assertFalse(self.db.exists())
        self.assertFalse(Path(str(self.snapshot) + ".receiver.lock").exists())

    def test_live_cli_corrects_initial_and_repeated_reads_and_child_server(self):
        self.raw = self.frame(time.time() + 13.5)
        port = self.free_port()
        log_path = self.root / "live.log"
        log = log_path.open("wb")
        self.addCleanup(log.close)
        process = subprocess.Popen([sys.executable, "-B", str(launcher.ROOT / "live_launcher.py"),
            "--robot-ip", "127.0.0.1", "--robot-port", str(self.camera.server_port),
            "--robot-clock-offset", "13.5", "--port", str(port),
            "--snapshot-file", str(self.snapshot), "--db", str(self.db),
            "--no-ssh", "--no-vla", "--no-browser", "--wait-seconds", "1"],
            cwd=self.root, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            stdout=log, stderr=log)

        def stop():
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

        self.addCleanup(stop)
        deadline = time.monotonic() + 6
        first = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(log_path.read_text())
            try:
                with self.opener.open("http://127.0.0.1:%d/api/telemetry" % port, timeout=.3) as response:
                    envelope = json.load(response)
                if envelope["connected"]:
                    first = envelope
                    break
            except OSError:
                pass
            time.sleep(.05)
        self.assertIsNotNone(first, log_path.read_text())
        self.assertEqual(first["robot_clock_offset_s"], 13.5)
        self.assertEqual(self.snapshot.read_bytes(), self.raw)
        updated = self.frame(time.time() + 13.5)
        self.raw = updated
        deadline = time.monotonic() + 3
        while self.snapshot.read_bytes() != updated and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(self.snapshot.read_bytes(), updated, log_path.read_text())
        self.assertIsNone(process.poll())
        stop()
        self.assertEqual(process.returncode, 0, log_path.read_text())
        self.assertEqual(launcher.probe(port), "absent")

    def test_invalid_offsets_fail_before_any_network_or_local_data_work(self):
        runtime = Runtime(self.db, interval=10)
        self.addCleanup(runtime.close)
        for value in (True, False, float("nan"), float("inf"), -float("inf"), 300.001, -300.001):
            calls = (
                (live.fetch_snapshot, (self.url,), {}),
                (make_handler, (runtime,), {}),
                (instance_identity, (self.root, self.db, 4), {}),
                (launcher.expected_identity, (), {}),
                (launcher.probe, (1,), {}),
                (launcher.ensure_server, (1,), {}),
            )
            for function, args, kwargs in calls:
                with self.subTest(value=value, function=function.__name__), self.assertRaises(ValueError):
                    self.clock_call(function, *args, robot_clock_offset=value, **kwargs)
        self.assertEqual(self.requests, [])

    def test_all_cli_entrypoints_reject_nonfinite_and_out_of_range_offsets(self):
        for script in ("server.py", "launcher.py", "live_launcher.py"):
            for value in ("nan", "inf", "-inf", "300.001", "-300.001"):
                with self.subTest(script=script, value=value):
                    result = self.run_script(script, "--robot-clock-offset=" + value, "--db", str(self.db))
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(b"robot-clock-offset", result.stderr)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
