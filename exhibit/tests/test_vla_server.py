import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Runtime, make_handler
from vla_evidence import VLAStore
from test_vla_evidence import event


class VLAServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Path(self.tmp.name) / 'sim.db', interval=10)
        self.store = VLAStore(Path(self.tmp.name) / 'vla.db')
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.runtime.close()
        self.store.close()
        self.tmp.cleanup()

    def start(self, configured=True):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.runtime, vla_store=self.store if configured else None))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def request(self, path, method='GET'):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request(method, path, body='{}' if method == 'POST' else None, headers={'Content-Type': 'application/json'})
        reply = conn.getresponse()
        result = reply.status, json.loads(reply.read())
        conn.close()
        return result

    def test_unconfigured_api_is_available(self):
        self.start(False)
        code, result = self.request('/api/vla/state')
        self.assertEqual(code, 200)
        self.assertFalse(result['configured'])
        self.assertFalse(result['connected'])
        self.assertEqual(result['status'], 'unconfigured')
        self.assertEqual(self.request('/api/vla/runs')[1], {'runs': []})

    def test_only_recorded_events_and_read_only_routes(self):
        self.store.ingest([event('status', 1, {'state': 'ready'})], 's', 10)
        self.start()
        self.assertEqual(self.request('/api/vla/state')[0], 200)
        code, result = self.request('/api/vla/events?run_id=run-1&after=0&limit=1')
        self.assertEqual(code, 200)
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(self.request('/api/vla/events?run_id=other')[1]['events'], [])
        self.assertEqual(self.request('/api/vla/events?run_id=run-1&after=wat')[0], 400)
        self.assertEqual(self.request('/api/vla/events?run_id=run-1&limit=100000')[0], 400)
        self.assertEqual(self.request('/api/vla/state', 'POST')[0], 404)
        self.assertEqual(self.store.cursor('s'), 10)
