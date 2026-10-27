import contextlib
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vla_evidence import VLAStore
from vla_receiver import Receiver, ReceiverError


def event(identifier="one", run="run-a"):
    return dict(schema_version=1, event_id=identifier, robot_id="robot239",
                session_id="session-a", run_id=run, operation_id="op-a",
                kind="operation_started", observed_at=time.time(),
                payload={"mode": "run", "task": "捡包布", "model_id": "R1"})


@contextlib.contextmanager
def source_http():
    state = dict(source_id="source-a", events=[event()], next_cursor=100,
                 has_more=False, mode="normal", requests=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            after = int(query.get("after", ["0"])[0])
            state["requests"].append(after)
            if state["mode"] == "redirect":
                self.send_response(302)
                self.send_header("Location", "/other")
                self.end_headers()
                return
            if state["mode"] == "failed":
                self.send_error(503)
                return
            if after > state["next_cursor"]:
                self.send_error(416)
                return
            body = dict(source_id=state["source_id"], next_cursor=state["next_cursor"],
                        has_more=state["has_more"],
                        events=state["events"] if after < state["next_cursor"] else [],
                        health={"observed_at": time.time(), "dropped": 0, "legacy": False})
            if state["mode"] == "invalid":
                body["events"] = [{"schema_version": 1}]
            if state["mode"] == "no_progress":
                body["next_cursor"] = after
                body["has_more"] = True
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw) + (9 if state["mode"] == "truncated" else 0)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%s" % server.server_port, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "vla.sqlite3"
        self.store = VLAStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_commits_cursor_and_events_then_restarts_without_duplicate(self):
        with source_http() as (url, state):
            receiver = Receiver(url, self.store)
            receiver.poll_once()
            self.assertEqual(self.store.cursor("source-a"), 100)
            receiver.poll_once()
            self.assertEqual(len(self.store.events("run-a")["events"]), 1)
            restarted = Receiver(url, self.store)
            restarted.poll_once()
            restarted.poll_once()
            self.assertEqual(state["requests"], [0, 100, 0, 100])
            self.assertEqual(len(self.store.events("run-a")["events"]), 1)

    def test_source_replacement_resets_offset_and_retains_old_history(self):
        with source_http() as (url, state):
            receiver = Receiver(url, self.store)
            receiver.poll_once()
            state.update(source_id="source-b", events=[event("two", "run-b")], next_cursor=50)
            receiver.poll_once()  # range reset, no cursor commit
            receiver.poll_once()
            self.assertEqual(self.store.cursor("source-b"), 50)
            self.assertEqual(len(self.store.events("run-a")["events"]), 1)
            self.assertEqual(len(self.store.events("run-b")["events"]), 1)

    def test_invalid_batch_does_not_advance_committed_cursor(self):
        with source_http() as (url, state):
            state["mode"] = "invalid"
            with self.assertRaises((ReceiverError, ValueError)):
                Receiver(url, self.store).poll_once()
            self.assertEqual(self.store.cursor("source-a"), 0)

    def test_truncated_or_redirected_body_is_not_accepted(self):
        with source_http() as (url, state):
            for mode in ("truncated", "redirect", "no_progress"):
                state["mode"] = mode
                with self.subTest(mode=mode), self.assertRaises(ReceiverError):
                    Receiver(url, self.store).poll_once()
                self.assertEqual(self.store.cursor("source-a"), 0)

    def test_background_receiver_recovers_and_stops_without_touching_source(self):
        with source_http() as (url, state):
            state["mode"] = "failed"
            receiver = Receiver(url, self.store, interval=0.02, timeout=0.5)
            receiver.start()
            try:
                deadline = time.monotonic() + 2
                while receiver.last_error is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertIsNotNone(receiver.last_error)
                state["mode"] = "normal"
                deadline = time.monotonic() + 2
                while self.store.cursor("source-a") != 100 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(self.store.cursor("source-a"), 100)
            finally:
                receiver.close()
            self.assertFalse(receiver.thread.is_alive())

    def test_rejects_credentials_and_non_http_sources(self):
        for url in ("file:///tmp/events", "http://user:secret@127.0.0.1:8774", "http://127.0.0.1/?secret=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Receiver(url, self.store)


if __name__ == "__main__":
    unittest.main()
