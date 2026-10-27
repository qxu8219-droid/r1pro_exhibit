"""Real local HTTP contract tests; no external endpoint or model is contacted."""

import json
import os
import threading
import time
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from planner import Planner


_CANDIDATES = [
    {"id": "pick-r1", "robot_id": "R1", "skill": "acquire_cart", "cart_id": "C17",
     "station_id": "SOURCE", "description": "取满车", "priority": 90},
    {"id": "wait-r2", "robot_id": "R2", "skill": "navigate", "station_id": "R2_WAIT",
     "description": "前往等待点", "priority": 10},
]
_SNAPSHOT = {"version": 4, "epoch": "demo-epoch", "sim_time": 2,
             "mission": {"goal": "送满车并返空车"}, "robots": [], "events": [{"secret": "omit-event"}]}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append({"path": self.path, "body": json.loads(body),
                                     "authorization": self.headers.get("Authorization")})
        if self.path == "/error":
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"untrusted provider details: test-secret")
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/valid")
            self.end_headers()
            return
        if self.path == "/slow":
            time.sleep(0.3)
        if self.path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"choices":')
            self.close_connection = True
            return
        if self.path == "/oversized":
            raw = b" " * (1024 * 1024 + 1)
        elif self.path == "/malformed":
            raw = b"not json"
        else:
            if self.path == "/unknown":
                selection = {"candidate_id": "invented-robot-action", "reason": "go"}
            elif self.path == "/null":
                selection = {"candidate_id": None, "reason": "现场需等待。"}
            elif self.path == "/secret":
                selection = {"candidate_id": "pick-r1", "reason": "test-secret"}
            elif self.path == "/extra":
                selection = {"candidate_id": "pick-r1", "reason": "取车", "command": "move"}
            else:
                selection = {"candidate_id": "pick-r1", "reason": "R1 可执行优先级较高的满车取车任务。"}
            content = json.dumps(selection, ensure_ascii=False)
            if self.path == "/markdown":
                content = "```json\n" + content + "\n```"
            elif self.path == "/duplicate":
                content = '{"candidate_id":"wait-r2","candidate_id":"pick-r1","reason":"go"}'
            raw = json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass


class PlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.daemon_threads = True
        cls.server.requests = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = "http://127.0.0.1:{}".format(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server.requests.clear()

    def planner(self, path="/valid", **overrides):
        os.environ.update({"FLEET_MODEL_URL": self.origin + path,
                           "FLEET_MODEL_NAME": "test-model", "FLEET_MODEL_API_KEY": "test-secret",
                           "FLEET_MODEL_TIMEOUT": "1"})
        os.environ.update(overrides)
        return Planner("model")

    def test_baseline_is_deterministic_and_never_calls_network(self):
        os.environ["FLEET_MODEL_URL"] = self.origin + "/valid"
        planner = Planner()
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("no network")):
            decision = planner.choose(_SNAPSHOT, list(reversed(_CANDIDATES)))
            self.assertEqual(decision["candidate_id"], "pick-r1")
            self.assertEqual(decision["mode"], "baseline")
            self.assertIn("规则基线", decision["reason"])
            self.assertIsNone(planner.choose(_SNAPSHOT, [])["candidate_id"])

    def test_baseline_tie_uses_id(self):
        candidates = [dict(_CANDIDATES[0], priority=10), _CANDIDATES[1]]
        self.assertEqual(Planner().choose(_SNAPSHOT, list(reversed(candidates)))["candidate_id"], "pick-r1")

    def test_valid_real_post_contract(self):
        decision = self.planner().choose(_SNAPSHOT, _CANDIDATES)
        self.assertEqual(decision["status"], "ok")
        self.assertEqual(decision["candidate_id"], "pick-r1")
        self.assertEqual(decision["mode"], "model")
        self.assertGreaterEqual(decision["latency_ms"], 0)
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/valid")
        self.assertEqual(request["authorization"], "Bearer test-secret")
        self.assertEqual(request["body"]["model"], "test-model")
        self.assertFalse(request["body"]["stream"])
        self.assertEqual(request["body"]["response_format"], {"type": "json_object"})
        sent = json.loads(request["body"]["messages"][1]["content"])
        self.assertEqual(sent["world"]["version"], 4)
        self.assertEqual(sent["candidates"], _CANDIDATES)
        self.assertNotIn("events", sent["world"])
        self.assertNotIn("test-secret", json.dumps(request["body"]))

    def test_four_robot_world_and_prompt_preserve_candidate_contract(self):
        from planner import _SYSTEM_PROMPT
        planner = self.planner()
        for count in (4, 2):
            with self.subTest(count=count):
                snapshot = dict(_SNAPSHOT, robots=[{"id": "R%d" % n} for n in range(1, count + 1)])
                decision = planner.choose(snapshot, _CANDIDATES)
                messages = self.server.requests[-1]["body"]["messages"]
                expected = _SYSTEM_PROMPT.replace("two-robot", "four-robot", 1) if count == 4 else _SYSTEM_PROMPT
                self.assertEqual(messages[0]["content"], expected)
                sent = json.loads(messages[1]["content"])
                self.assertEqual(sent["world"]["robots"], snapshot["robots"])
                self.assertEqual(sent["candidates"], _CANDIDATES)
                self.assertEqual(decision["candidate_id"], "pick-r1")

    def test_compatible_endpoint_toggle_and_optional_local_key(self):
        planner = self.planner(FLEET_MODEL_JSON_FORMAT="0", FLEET_MODEL_API_KEY="")
        self.assertTrue(planner.config_status()["configured"])
        self.assertEqual(planner.choose(_SNAPSHOT, _CANDIDATES)["status"], "ok")
        self.assertNotIn("response_format", self.server.requests[0]["body"])
        self.assertIsNone(self.server.requests[0]["authorization"])

    def test_null_selection_is_explicit_wait(self):
        result = self.planner("/null").choose(_SNAPSHOT, _CANDIDATES)
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["candidate_id"])

    def test_invalid_responses_fail_closed(self):
        for path in ("/malformed", "/unknown", "/markdown", "/duplicate", "/extra"):
            with self.subTest(path=path):
                result = self.planner(path).choose(_SNAPSHOT, _CANDIDATES)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["mode"], "model")
                self.assertIsNone(result["candidate_id"])

    def test_http_error_does_not_echo_response(self):
        result = self.planner("/error").choose(_SNAPSHOT, _CANDIDATES)
        self.assertEqual(result["status"], "error")
        self.assertIn("503", result["error"])
        self.assertNotIn("test-secret", json.dumps(result))

    def test_redirect_is_not_followed(self):
        result = self.planner("/redirect").choose(_SNAPSHOT, _CANDIDATES)
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(self.server.requests), 1)
        self.assertIn("302", result["error"])

    def test_truncated_and_oversized_responses_fail_closed(self):
        for path in ("/truncated", "/oversized"):
            with self.subTest(path=path):
                result = self.planner(path).choose(_SNAPSHOT, _CANDIDATES)
                self.assertEqual(result["status"], "error")
                self.assertIsNone(result["candidate_id"])

    def test_timeout_fails_closed(self):
        result = self.planner("/slow", FLEET_MODEL_TIMEOUT="0.1").choose(_SNAPSHOT, _CANDIDATES)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["candidate_id"])
        self.assertIn("超时", result["error"])

    def test_missing_configuration_does_not_fallback(self):
        result = Planner("model").choose(_SNAPSHOT, _CANDIDATES)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["candidate_id"])
        self.assertEqual(self.server.requests, [])

    def test_no_candidates_makes_no_call(self):
        result = self.planner().choose(_SNAPSHOT, [])
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["candidate_id"])
        self.assertIn("未调用模型", result["reason"])
        self.assertEqual(self.server.requests, [])

    def test_status_and_summary_redact_key(self):
        planner = self.planner("/secret")
        status = planner.config_status()
        self.assertEqual(status["endpoint"], self.origin)
        self.assertTrue(status["api_key_present"])
        self.assertNotIn("test-secret", json.dumps(status))
        self.assertNotIn("test-secret", json.dumps(planner.choose(_SNAPSHOT, _CANDIDATES)))

    def test_bad_config_and_remote_plain_http_fail_before_network(self):
        invalid = (
            {"FLEET_MODEL_URL": "http://example.com/chat"},
            {"FLEET_MODEL_URL": "https://user:pass@example.com/chat"},
            {"FLEET_MODEL_URL": "https://example.com/chat?api_key=secret"},
            {"FLEET_MODEL_URL": "https://example.com/chat", "FLEET_MODEL_API_KEY": ""},
            {"FLEET_MODEL_TIMEOUT": "nan"},
            {"FLEET_MODEL_TIMEOUT": "0"},
            {"FLEET_MODEL_TIMEOUT": "121"},
            {"FLEET_MODEL_JSON_FORMAT": "maybe"},
            {"FLEET_MODEL_NAME": ""},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                planner = self.planner(**overrides)
                self.assertFalse(planner.config_status()["configured"])
                self.assertEqual(planner.choose(_SNAPSHOT, _CANDIDATES)["status"], "error")
        self.assertEqual(self.server.requests, [])

    def test_invalid_input_candidates_do_not_dispatch(self):
        for candidates in ([{"id": "duplicate"}, {"id": "duplicate"}],
                           [{"id": "bad-priority", "priority": True}], [None], "invalid"):
            with self.subTest(candidates=candidates):
                result = Planner().choose(_SNAPSHOT, candidates)
                self.assertEqual(result["status"], "error")
                self.assertIsNone(result["candidate_id"])

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            Planner("pretend-model")


if __name__ == "__main__":
    unittest.main()
