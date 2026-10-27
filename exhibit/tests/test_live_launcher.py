"""Real HTTP and process checks for the local-only camera launcher."""
import base64
import importlib.util
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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher
live = __import__('live_launcher') if importlib.util.find_spec('live_launcher') else None


class LiveLaunchTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(live, 'Local live launcher is not implemented')
        self.temp = tempfile.TemporaryDirectory(prefix='R1 live startup ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / 'camera snapshot.json'
        self.override = None
        self.status = 200
        self.broken_chunks = False
        self.requests = []
        test = self

        class Camera(BaseHTTPRequestHandler):
            def do_GET(self):
                test.requests.append(self.path)
                if test.broken_chunks:
                    self.send_response(200)
                    self.send_header('Transfer-Encoding','chunked')
                    self.end_headers()
                    self.wfile.write(b'20\r\n{}')
                    return
                body = test.override if test.override is not None else json.dumps(test.frame()).encode()
                self.send_response(test.status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def log_message(self, *_):
                pass

        self.camera = ThreadingHTTPServer(('127.0.0.1', 0), Camera)
        self.thread = threading.Thread(target=self.camera.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_camera)
        self.url = 'http://127.0.0.1:%d/snapshot' % self.camera.server_port

    def stop_camera(self):
        self.camera.shutdown()
        self.camera.server_close()
        self.thread.join(2)

    def frame(self):
        image = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()
        return dict(schema_version=1, robot_id='qa-robot', run_id='qa-run',
                    task='camera test', model_id='no model', stage='camera',
                    observed_at=time.time(), images={key:image for key in ('head','wrist_left','wrist_right')})

    def test_fetch_bypasses_proxy_and_keeps_source_timestamp(self):
        original = self.frame()
        self.override = json.dumps(original).encode()
        with patch.dict(os.environ, {'http_proxy':'http://127.0.0.1:1','HTTP_PROXY':'http://127.0.0.1:1','NO_PROXY':'','no_proxy':''}):
            self.assertEqual(live.fetch_snapshot(self.url), self.override)
        self.assertEqual(self.requests, ['/snapshot'])
        self.assertEqual(json.loads(self.override)['observed_at'], original['observed_at'])

    def test_rejects_bad_http_json_incomplete_cameras_and_bad_clock(self):
        bad_values = [b'{}', b'{broken', b'{"x":NaN}']
        for age in (10, -10):
            value = self.frame(); value['observed_at'] -= age
            bad_values.append(json.dumps(value).encode())
        value = self.frame(); del value['images']['wrist_right']
        bad_values.append(json.dumps(value).encode())
        for value in bad_values:
            with self.subTest(value=value[:40]):
                self.override = value
                with self.assertRaises(live.LiveError):
                    live.fetch_snapshot(self.url)
        self.status = 500
        with self.assertRaises(live.LiveError):
            live.fetch_snapshot(self.url)

    def test_atomic_write_preserves_bytes_and_does_not_leave_temp_files(self):
        value = json.dumps(self.frame()).encode()
        live.atomic_write(self.snapshot, value)
        self.assertEqual(self.snapshot.read_bytes(), value)
        second = value + b'\n'
        live.atomic_write(self.snapshot, second)
        self.assertEqual(self.snapshot.read_bytes(), second)
        self.assertEqual(list(self.root.iterdir()), [self.snapshot])

    def test_empty_bridge_snapshot_reports_camera_not_ready(self):
        self.override=b'{}'
        with self.assertRaisesRegex(live.LiveError, '尚未就绪'):
            live.fetch_snapshot(self.url)

    def test_truncated_http_response_is_a_retryable_receiver_error(self):
        self.broken_chunks=True
        with self.assertRaises(live.LiveError):
            live.fetch_snapshot(self.url)

    def test_receiver_lock_prevents_duplicate_writer_and_can_be_reacquired(self):
        with live.receiver_lock(self.snapshot):
            with self.assertRaises(live.LiveError):
                with live.receiver_lock(self.snapshot):
                    self.fail('Duplicate writer entered')
        with live.receiver_lock(self.snapshot):
            pass

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            return sock.getsockname()[1]

    def command(self, port):
        return ['sh',str(launcher.ROOT.parent/'start_live.sh'),
                '--robot-ip','127.0.0.1','--robot-port',str(self.camera.server_port),
                '--port',str(port),'--snapshot-file',str(self.snapshot),
                '--db',str(self.root/'fleet.sqlite3'),'--no-browser','--wait-seconds','1']

    def start(self, port):
        log = (self.root/'launcher.log').open('ab')
        self.addCleanup(log.close)
        process = subprocess.Popen(self.command(port), cwd=self.root,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), stdout=log, stderr=log)
        self.addCleanup(self.stop, process)
        return process

    def stop(self, process):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=2)

    def wait_live(self, port, process):
        until = time.monotonic()+8
        opener = build_opener(ProxyHandler({}))
        while time.monotonic()<until:
            if process.poll() is not None:
                self.fail((self.root/'launcher.log').read_text())
            try:
                with opener.open('http://127.0.0.1:%d/api/telemetry'%port,timeout=.5) as response:
                    result=json.load(response)
                if result['connected']:
                    return result
            except OSError:
                pass
            time.sleep(.05)
        self.fail('Live source did not become ready')

    def test_check_only_does_not_write_local_data_or_start_server(self):
        result = subprocess.run(self.command(self.free_port())+['--check'], cwd=self.root,
                    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse(self.snapshot.exists())
        self.assertFalse((self.root/'fleet.sqlite3').exists())

    def test_shell_launch_receives_recovers_and_stops_only_owned_process(self):
        port=self.free_port()
        process=self.start(port)
        self.assertEqual(self.wait_live(port,process)['state']['run_id'],'qa-run')
        self.override=b'broken'
        time.sleep(.15)
        original=self.snapshot.read_bytes()
        time.sleep(1.3)
        self.assertEqual(self.snapshot.read_bytes(),original,'invalid response must not replace last real observation')
        duplicate=subprocess.run(self.command(port),cwd=self.root,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),capture_output=True,timeout=5)
        self.assertNotEqual(duplicate.returncode,0)
        self.assertIsNone(process.poll())
        self.override=None
        before=json.loads(original)['observed_at']
        until=time.monotonic()+3
        while json.loads(self.snapshot.read_bytes())['observed_at']<=before and time.monotonic()<until:
            time.sleep(.05)
        self.assertGreater(json.loads(self.snapshot.read_bytes())['observed_at'],before)
        self.stop(process)
        self.assertEqual(process.returncode,0)
        self.assertEqual(launcher.probe(port), 'absent')

    def test_exiting_receiver_preserves_reused_server(self):
        port=self.free_port()
        server=launcher.ensure_server(port,self.root/'fleet.sqlite3',telemetry_file=self.snapshot)
        self.addCleanup(launcher.stop_owned_server,server)
        process=self.start(port)
        self.wait_live(port,process)
        self.stop(process)
        self.assertEqual(process.returncode,0)
        self.assertIsNone(server.poll())
        self.assertEqual(launcher.probe(port,self.root/'fleet.sqlite3',self.snapshot),'exhibit')

    def test_writer_lock_stays_held_until_owned_server_is_stopped(self):
        # Exercise real service startup/shutdown. The browser boundary injects
        # Ctrl+C; a second receiver must remain excluded during child cleanup.
        original_stop=live.stop_owned_server
        lock_states=[]

        def stop_and_observe(process):
            if process is not None:
                try:
                    with live.receiver_lock(self.snapshot):
                        lock_states.append(False)
                except live.LiveError:
                    lock_states.append(True)
            original_stop(process)

        args=self.command(self.free_port())[2:]
        args.remove('--no-browser')
        with patch.object(live,'open_browser',side_effect=KeyboardInterrupt), patch.object(live,'stop_owned_server',side_effect=stop_and_observe):
            self.assertEqual(live.main(args),0)
        self.assertTrue(lock_states)
        self.assertTrue(all(lock_states),'lock must not be released before owned service cleanup')


if __name__=='__main__':
    unittest.main()
