import json
import http.client
import mimetypes
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import Runtime, make_handler
from instance_identity import instance_identity


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Path(self.temp.name) / "test.sqlite3", interval=10)
        self.server = ThreadingHTTPServer(("0.0.0.0", 0), make_handler(self.runtime))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.runtime.close()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None, address="127.0.0.1"):
        connection = http.client.HTTPConnection(address, self.server.server_port, timeout=3)
        try:
            connection.request("GET" if body is None else "POST", path,
                               body=None if body is None else json.dumps(body).encode(),
                               headers=headers or ({"Content-Type": "application/json"} if body is not None else {}))
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def test_assets_and_actual_api_complete_simulated_mission(self):
        for path in ("/", "/app.js", "/style.css", "/api/health"):
            self.assertEqual(self.request(path)[0], 200)
        state = None
        for _ in range(150):
            code, _, raw = self.request("/api/control", {"action": "step"})
            self.assertEqual(code, 200, raw)
            state = json.loads(raw)["state"]
            if state["mission"]["status"] == "SUCCEEDED":
                break
        self.assertEqual(state["mission"]["status"], "SUCCEEDED", state["mission"])
        cart = next(c for c in state["carts"] if c["id"] == "C17")
        self.assertEqual((cart["load"], cart["station_id"], cart["held_by"]), ("empty", "SOURCE", None))
        self.assertTrue(all(not r["held_cart_id"] for r in state["robots"]))
        self.assertTrue(all(not s["reserved_by"] for s in state["stations"]))

    def test_local_origin_json_and_host_controls(self):
        code, _, _ = self.request("/api/control", {"action": "reset"}, {"Content-Type": "application/json", "Origin": "https://example.com"})
        self.assertEqual(code, 403)
        code, _, _ = self.request("/api/control", {"action": "reset"}, {"Content-Type": "text/plain"})
        self.assertEqual(code, 415)
        code, _, _ = self.request("/api/state", headers={"Host": "evil.invalid"})
        self.assertEqual(code, 403)
        self.assertEqual(self.request("/api/control", {"action": "not-a-command"})[0], 400)

    def test_non_localhost_destination_serves_page_assets_and_api(self):
        # A second loopback address exercises wildcard binding without depending
        # on a particular Wi-Fi interface or reaching another computer.
        for path in ("/", "/app.js", "/showcase.js", "/api/health", "/api/replays"):
            with self.subTest(path=path):
                code, _, raw = self.request(path, address="127.0.0.2")
                self.assertEqual(code, 200, raw)

    def test_non_localhost_destination_accepts_same_origin_control(self):
        origin = "http://127.0.0.2:%d" % self.server.server_port
        code, _, raw = self.request("/api/control", {"action": "pause"},
                                    {"Content-Type": "application/json", "Origin": origin},
                                    address="127.0.0.2")
        self.assertEqual(code, 200, raw)
        self.assertFalse(json.loads(raw)["state"]["runtime"]["running"])

    def test_non_localhost_destination_rejects_cross_origin_control(self):
        code, _, raw = self.request("/api/control", {"action": "pause"},
                                    {"Content-Type": "application/json", "Origin": "https://evil.invalid"},
                                    address="127.0.0.2")
        self.assertEqual(code, 403)
        self.assertEqual(json.loads(raw)["error"], "CROSS_ORIGIN_REQUEST_REJECTED")

    def test_non_localhost_destination_does_not_trust_arbitrary_host(self):
        port = self.server.server_port
        for host in ("evil.invalid:%d" % port, "127.0.0.3:%d" % port,
                     "0.0.0.0:%d" % port, "127.0.0.2:%d" % (port + 1)):
            with self.subTest(host=host):
                self.assertEqual(self.request("/api/state", headers={"Host": host},
                                              address="127.0.0.2")[0], 403)

    def test_health_exposes_configuration_digest_without_filesystem_paths(self):
        code, _, raw = self.request("/api/health")
        value = json.loads(raw)
        self.assertEqual(code, 200)
        self.assertEqual(value["instance_identity"], instance_identity(
            Path(__file__).resolve().parents[1], self.runtime.db_path, self.runtime.fleet_size))
        self.assertRegex(value["instance_identity"], r"^[0-9a-f]{64}$")
        self.assertEqual(set(value), {"ok", "simulation", "fleet_size", "instance_identity"})
        self.assertNotIn(self.temp.name, raw.decode("utf-8"))

    def test_static_mime_is_independent_of_platform_registry(self):
        paths = {"/": "text/html", "/app.js": "text/javascript", "/showcase.js": "text/javascript",
                 "/style.css": "text/css", "/showcase.css": "text/css"}
        replays = Path(__file__).resolve().parents[1] / "static" / "replays"
        for replay in replays.glob("*.html"):
            paths["/replays/" + replay.name] = "text/html"
        with patch.object(mimetypes, "guess_type", return_value=("text/plain", None)):
            for path, expected in paths.items():
                with self.subTest(path=path):
                    code, headers, _ = self.request(path)
                    self.assertEqual(code, 200)
                    self.assertEqual(headers.get("Content-Type"), expected + "; charset=utf-8")
                    self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_pause_discards_inflight_model_decision(self):
        self._superseded_decision("pause")

    def test_reset_discards_inflight_model_decision(self):
        self._superseded_decision("reset")

    def _superseded_decision(self, action):
        started = threading.Event()
        release = threading.Event()

        class DelayedPlanner:
            mode = "model"

            def choose(self, state, candidates):
                started.set()
                release.wait(timeout=3)
                return {"candidate_id": candidates[0]["id"], "status": "ok", "mode": "model", "reason": "测试提案", "latency_ms": 0}

        self.runtime.planner = DelayedPlanner()
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(self.runtime.advance()))
        thread.start()
        self.assertTrue(started.wait(timeout=2))
        self.runtime.control({"action": action})
        release.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes[0]["code"], "DECISION_SUPERSEDED")
        self.assertEqual(self.runtime.engine.snapshot()["tasks"], [])


if __name__ == "__main__":
    unittest.main()
