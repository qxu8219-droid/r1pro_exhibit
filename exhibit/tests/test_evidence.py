import base64
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence import EvidenceError, read_recording, read_telemetry
from server import Runtime, make_handler


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.replays = self.root / "replays"
        self.replays.mkdir()
        self.file = self.root / "snapshot.json"
        self.now = 1792560000.0

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, **updates):
        value = {"schema_version": 1, "robot_id": "R1-Pro-239", "run_id": "run-001",
                 "observed_at": self.now - 1, "task": "抓取物资车", "model_id": "g0.5-demo",
                 "stage": "grasp", "images": {}, "request_id": 7, "rtt_ms": 123.0}
        value.update(updates)
        self.file.write_text(json.dumps(value), encoding="utf-8")
        return value

    def test_recording_is_json_only_and_preserves_data(self):
        data = {"run_id": "recorded", "images": [{"camera": "head"}], "text": "</script>"}
        page = "<script>const DATA = " + json.dumps(data) + "; throw new Error('never run');</script>"
        (self.replays / "sample.html").write_text(page, encoding="utf-8")
        self.assertEqual(read_recording(self.replays, "sample.html"),
                         {"source": "recording", "file": "sample.html", "data": data})

    def test_recording_rejects_paths_and_external_symlink(self):
        for name in ("../outside.html", "/tmp/outside.html", "..\\outside.html", "C:\\outside.html", "bad\x00.html", "x.json", ""):
            with self.subTest(name=name), self.assertRaises(EvidenceError) as caught:
                read_recording(self.replays, name)
            self.assertEqual(caught.exception.status, 400)
        outside = self.root / "outside.html"
        outside.write_text("const DATA = {};", encoding="utf-8")
        link = self.replays / "linked.html"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            return  # Windows may require privileges to create symlinks.
        with self.assertRaises(EvidenceError) as caught:
            read_recording(self.replays, link.name)
        self.assertEqual(caught.exception.status, 400)

    def test_recording_missing_corrupt_oversize_and_non_file(self):
        with self.assertRaises(EvidenceError) as caught:
            read_recording(self.replays, "missing.html")
        self.assertEqual(caught.exception.status, 404)
        path = self.replays / "sample.html"
        for content in ("no data", "const DATA = function() {};", "const DATA = null;",
                        "const DATA = {} || evil();", "const DATA = {\"n\":NaN};",
                        "const DATA = {\"n\":1e999};"):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(EvidenceError) as caught:
                read_recording(self.replays, path.name)
            self.assertEqual(caught.exception.status, 422)
        path.write_text("const DATA = {};", encoding="utf-8")
        with patch("evidence.MAX_REPLAY_BYTES", 5), self.assertRaises(EvidenceError) as caught:
            read_recording(self.replays, path.name)
        self.assertEqual(caught.exception.status, 413)
        path.unlink()
        path.mkdir()
        with self.assertRaises(EvidenceError) as caught:
            read_recording(self.replays, path.name)
        self.assertEqual(caught.exception.code, "INVALID_FILE")

    def test_bundled_recordings_parse_without_touching_source(self):
        replays = Path(__file__).resolve().parents[1] / "static" / "replays"
        for path in replays.glob("*.html"):
            before = path.stat().st_mtime_ns
            with self.subTest(file=path.name):
                result = read_recording(replays, path.name)
                self.assertEqual(result["source"], "recording")
                self.assertIsInstance(result["data"], dict)
                self.assertEqual(path.stat().st_mtime_ns, before)

    def test_default_unconfigured_and_missing_file_remain_disconnected(self):
        default = read_telemetry()
        self.assertFalse(default["configured"])
        self.assertFalse(default["connected"])
        self.assertIsNone(default["state"])
        result = read_telemetry(self.file, self.now)
        self.assertTrue(result["configured"])
        self.assertEqual(result["status"], "missing")
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertFalse(self.file.exists())

    def test_fresh_snapshot_is_whitelisted_and_read_only(self):
        self.snapshot(secret="must not leave file", debug={"endpoint": "private"})
        before = self.file.read_bytes()
        mtime = self.file.stat().st_mtime_ns
        result = read_telemetry(self.file, self.now)
        self.assertTrue(result["connected"])
        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["age_s"], 1)
        self.assertEqual(result["state"]["robot_id"], "R1-Pro-239")
        self.assertNotIn("secret", result["state"])
        self.assertNotIn("debug", result["state"])
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.file.stat().st_mtime_ns, mtime)

    def test_stale_and_future_snapshots_keep_identity_but_disconnect(self):
        for age in (-1, 5.001, 10000):
            self.snapshot(observed_at=self.now - age)
            with self.subTest(age=age):
                result = read_telemetry(self.file, self.now)
                self.assertEqual(result["status"], "stale")
                self.assertFalse(result["connected"])
                self.assertEqual(result["state"]["run_id"], "run-001")
                self.assertEqual(result["age_s"], age)
        for age in (0, 5):
            self.snapshot(observed_at=self.now - age)
            self.assertTrue(read_telemetry(self.file, self.now)["connected"])

    def test_invalid_identity_schema_timestamp_and_rtt(self):
        cases = [{"robot_id": ""}, {"run_id": None}, {"robot_id": "x" * 129},
                 {"model_id": ""}, {"stage": []}, {"task": "x" * 1001},
                 {"schema_version": True}, {"schema_version": 2},
                 {"observed_at": True}, {"observed_at": 0}, {"observed_at": "2026-10-21"},
                 {"observed_at": float("nan")}, {"observed_at": float("inf")},
                 {"rtt_ms": -1}, {"rtt_ms": float("inf")}, {"request_id": True},
                 {"request_id": -1}, {"images": []}]
        for updates in cases:
            with self.subTest(updates=updates):
                self.snapshot(**updates)
                result = read_telemetry(self.file, self.now)
                self.assertEqual(result["status"], "invalid")
                self.assertFalse(result["connected"])
                self.assertIsNone(result["state"])

    def test_inline_images_only_and_no_extra_camera_fields(self):
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii")
        jpeg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xfffixture").decode("ascii")
        self.snapshot(images={"head": png, "wrist_left": jpeg, "debug": "https://private.example"})
        result = read_telemetry(self.file, self.now)
        self.assertTrue(result["connected"])
        self.assertEqual(result["state"]["images"], {"head": png, "wrist_left": jpeg})
        for image in ("https://example.com/frame.jpg", "/local/frame.png", "file:///tmp/p.png",
                      "data:image/svg+xml;base64,PHN2Zy8+", "data:image/png;base64,!!", "data:image/png;base64,aGVsbG8="):
            self.snapshot(images={"head": image})
            with self.subTest(image=image):
                self.assertEqual(read_telemetry(self.file, self.now)["status"], "invalid")
        self.snapshot(images={"head": png})
        with patch("evidence.MAX_IMAGE_CHARS", 10):
            self.assertEqual(read_telemetry(self.file, self.now)["status"], "invalid")

    def test_missing_images_are_allowed(self):
        value = self.snapshot()
        del value["images"]
        self.file.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(read_telemetry(self.file, self.now)["state"]["images"], {})

    def test_damaged_oversize_nonregular_and_extreme_json_do_not_crash(self):
        for content in ("{", "null", "[]", '{"x":1e999}', '{"x":NaN}', '[' * 1500):
            self.file.write_text(content, encoding="utf-8")
            with self.subTest(content=content[:20]):
                self.assertEqual(read_telemetry(self.file, self.now)["status"], "invalid")
        self.snapshot()
        with patch("evidence.MAX_TELEMETRY_BYTES", 10):
            self.assertEqual(read_telemetry(self.file, self.now)["status"], "too_large")
        self.assertEqual(read_telemetry(self.root, self.now)["status"], "invalid")
        if hasattr(os, "mkfifo"):
            fifo = self.root / "pipe"
            os.mkfifo(fifo)
            self.assertEqual(read_telemetry(fifo, self.now)["status"], "invalid")


class EvidenceServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.telemetry = self.root / "snapshot.json"
        self.runtime = Runtime(self.root / "state.sqlite3", interval=10)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.runtime, telemetry_file=self.telemetry))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.runtime.close()
        self.temp.cleanup()

    def request(self, path, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body="{}" if method == "POST" else None,
                               headers=headers or ({"Content-Type": "application/json"} if method == "POST" else {}))
            response = connection.getresponse()
            return response.status, response.headers, json.loads(response.read())
        finally:
            connection.close()

    def test_recording_endpoint_contract_errors_and_host(self):
        code, _, data = self.request("/api/replays")
        self.assertEqual(code, 200)
        self.assertEqual(set(data), {"replays"})
        code, headers, evidence = self.request("/api/evidence?file=" + data["replays"][0])
        self.assertEqual(code, 200)
        self.assertEqual(evidence["source"], "recording")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("/api/evidence?file=missing.html")[0], 404)
        for path in ("/api/evidence", "/api/evidence?file=", "/api/evidence?file=a.html&file=b.html",
                     "/api/evidence?file=..%2Fsecret.html", "/api/evidence?file=%2Ftmp%2Fsecret.html"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request("/api/evidence?file=missing.html", headers={"Host": "remote.invalid"})[0], 403)

    def test_telemetry_get_cannot_mutate_fleet_or_write_snapshot(self):
        before = self.runtime.state()
        code, _, data = self.request("/api/telemetry")
        self.assertEqual(code, 200)
        self.assertEqual(data["status"], "missing")
        self.assertFalse(self.telemetry.exists())
        value = {"schema_version": 1, "robot_id": "R1-239", "run_id": "run-a", "observed_at": time.time(),
                 "task": "抓放", "model_id": "g0.5", "stage": "grasp"}
        self.telemetry.write_text(json.dumps(value), encoding="utf-8")
        raw = self.telemetry.read_bytes()
        code, _, data = self.request("/api/telemetry")
        self.assertEqual(code, 200)
        self.assertTrue(data["connected"])
        self.assertEqual(self.request("/api/telemetry", method="POST")[0], 404)
        self.assertEqual(self.telemetry.read_bytes(), raw)
        self.assertEqual(self.runtime.state(), before)
        self.assertNotIn(str(self.root), json.dumps(data))


if __name__ == "__main__":
    unittest.main()
