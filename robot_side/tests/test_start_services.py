"""Exercise the deployed entry point against isolated local HTTP processes."""
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest


SOURCE = Path(__file__).resolve().parents[1]
CAMERA = r'''
import argparse, json, os, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--output');p.add_argument('--http-port',type=int);p.add_argument('--robot-id')
a=p.parse_args()
record=dict(pid=os.getpid(),cwd=os.getcwd(),sid=os.getsid(0),output=a.output,
            env={k:os.environ.get(k) for k in ('PYTHONDONTWRITEBYTECODE','XDG_CACHE_HOME','ROS_LOG_DIR','ROS_HOME','GST_REGISTRY','TMPDIR')})
Path('started.json').write_text(json.dumps(record))
if os.environ.get('TEST_CAMERA_MODE')=='exit':raise SystemExit(7)
def snapshot():
    if os.environ.get('TEST_CAMERA_MODE')=='pending':return {}
    return dict(schema_version=1,robot_id=a.robot_id,run_id='real-fixture',observed_at=time.time(),
                images={k:'data:image/jpeg;base64,ZmFrZQ==' for k in ('head','wrist_left','wrist_right')})
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data=snapshot();payload=json.dumps(data).encode()
        Path(a.output).write_bytes(payload)
        if self.path=='/healthz':payload=json.dumps(dict(ok=True,version=1,updated=time.time(),bytes=len(payload))).encode()
        elif self.path!='/snapshot':self.send_error(404);return
        self.send_response(200);self.end_headers();self.wfile.write(payload)
    def log_message(self,*args):pass
HTTPServer(('127.0.0.1',a.http_port),Handler).serve_forever()
'''


def unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def fingerprint(root):
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob('*') if p.is_file()}


@contextlib.contextmanager
def listener(health, snapshot=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = health if self.path == '/healthz' else snapshot
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ServicesStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.bundle = self.base / 'auto-services'
        self.bundle.mkdir()
        for name in ('start_services.py', 'start_observer.sh', 'bridge.py'):
            if (SOURCE / name).exists():
                shutil.copyfile(SOURCE / name, self.bundle / name)
        self.original = self.base / 'original'
        self.original.mkdir()
        self.legacy = self.original / 'runtime'
        self.legacy.mkdir()
        (self.legacy / 'keep.txt').write_text('untouched original data')
        self.camera_script = self.original / 'run_live_bridge.sh'
        self.camera_script.write_text('exec "$VLA_PYTHON" -B "$(dirname "$0")/camera.py" "$@"\n')
        (self.original / 'camera.py').write_text(CAMERA)
        self.original_before = fingerprint(self.original)
        self.camera_port, self.vla_port = unused_port(), unused_port()
        while self.vla_port == self.camera_port:
            self.vla_port = unused_port()
        self.env = dict(os.environ, VLA_PYTHON=sys.executable,
                        VLA_CAMERA_SCRIPT=str(self.camera_script), VLA_LEGACY_ROOT=str(self.legacy),
                        PYTHONDONTWRITEBYTECODE='1')

    def tearDown(self):
        # Only terminate subprocesses created by this test, never other listeners.
        pids = set()
        for path in self.bundle.rglob('*.pid'):
            try:
                pids.add(int(path.read_text()))
            except (ValueError, OSError):
                pass
        for path in self.bundle.rglob('started.json'):
            try:
                pids.add(json.loads(path.read_text())['pid'])
            except (ValueError, OSError):
                pass
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.temp.cleanup()

    def command(self, *args):
        return [sys.executable, '-B', str(self.bundle / 'start_services.py'),
                '--camera-port', str(self.camera_port), '--vla-port', str(self.vla_port),
                '--startup-timeout', '1.5', *args]

    def run_launcher(self, *args, env=None):
        return subprocess.run(self.command(*args), cwd=self.base,
                              env=env or self.env, capture_output=True, text=True, timeout=12)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['status'], 'ready')
        return data

    def test_start_and_reuse_preserve_sources_and_keep_all_outputs_under_default_root(self):
        first = self.assert_success(self.run_launcher())
        self.assertEqual(first['services']['camera']['status'], 'started')
        self.assertEqual(first['services']['vla']['status'], 'started')
        runs = list((self.bundle / 'runs').iterdir())
        self.assertEqual(len(runs), 1)
        camera = json.loads((runs[0] / 'camera/started.json').read_text())
        self.assertEqual(Path(camera['cwd']), runs[0] / 'camera')
        self.assertEqual(camera['pid'], camera['sid'])
        self.assertEqual(camera['env']['PYTHONDONTWRITEBYTECODE'], '1')
        for name, value in camera['env'].items():
            if name != 'PYTHONDONTWRITEBYTECODE':
                self.assertTrue(Path(value).is_relative_to(runs[0]), (name, value))
        self.assertEqual(Path(camera['output']), runs[0] / 'camera/snapshot.json')
        self.assertTrue((runs[0] / 'vla/runtime/observer.pid').exists())
        second = self.assert_success(self.run_launcher())
        for service in ('camera', 'vla'):
            self.assertEqual(second['services'][service]['status'], 'reused')
        self.assertEqual(first['services']['vla']['pid'], second['services']['vla']['pid'])
        self.assertEqual(list((self.bundle / 'runs').iterdir()), runs)
        self.assertEqual(fingerprint(self.original), self.original_before)
        self.assertEqual({p.name for p in self.base.iterdir()}, {'auto-services', 'original'})
        self.assertFalse(list(self.base.rglob('__pycache__')))

    def test_camera_only_does_not_start_observer_or_require_legacy_source(self):
        env = dict(self.env, VLA_LEGACY_ROOT=str(self.base / 'absent'))
        data = self.assert_success(self.run_launcher('--camera-only', env=env))
        self.assertEqual(set(data['services']), {'camera'})
        self.assertFalse(list(self.bundle.rglob('observer.pid')))
        self.assertFalse((self.base / 'absent').exists())

    def test_camera_port_conflict_is_left_running_without_a_run(self):
        with listener({'unrelated': True}) as self.camera_port:
            result = self.run_launcher()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('conflict', result.stderr.lower())
            self.assertFalse((self.bundle / 'runs').exists())
            with socket.create_connection(('127.0.0.1', self.camera_port), timeout=1):
                pass

    def test_observer_conflict_prevents_starting_camera(self):
        with listener({'service': 'other-service'}) as self.vla_port:
            result = self.run_launcher()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('conflict', result.stderr.lower())
            self.assertFalse((self.bundle / 'runs').exists())

    def test_broken_chunked_http_listener_is_reported_as_conflict(self):
        class BrokenHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                self.wfile.write(b'not-a-chunk-size\r\n')

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), BrokenHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.camera_port = server.server_port
            result = self.run_launcher('--camera-only')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('conflict', result.stderr.lower())
            self.assertEqual(json.loads(result.stdout)['status'], 'conflict')
            self.assertFalse((self.bundle / 'runs').exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_wrong_robot_camera_is_not_reused_or_restarted(self):
        snapshot = dict(schema_version=1, robot_id='other-robot', observed_at=1,
                        images={key: 'data:image/jpeg;base64,eA==' for key in ('head', 'wrist_left', 'wrist_right')})
        with listener(dict(ok=True, version=1, updated=1, bytes=99), snapshot) as self.camera_port:
            result = self.run_launcher('--camera-only')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('conflict', result.stderr.lower())
            self.assertFalse((self.bundle / 'runs').exists())

    def test_out_of_range_health_timestamp_is_conflict(self):
        with listener(dict(ok=True, version=0, updated=10 ** 400, bytes=2), {}) as self.camera_port:
            result = self.run_launcher('--camera-only')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('conflict', result.stderr.lower())
            self.assertEqual(json.loads(result.stdout)['status'], 'conflict')
            self.assertFalse((self.bundle / 'runs').exists())

    def test_boolean_schema_version_is_not_a_verified_camera_snapshot(self):
        snapshot = dict(schema_version=True, robot_id='R1Pro2026_001', observed_at=1,
                        images={key: 'data:image/jpeg;base64,eA==' for key in ('head', 'wrist_left', 'wrist_right')})
        with listener(dict(ok=True, version=1, updated=1, bytes=99), snapshot) as self.camera_port:
            result = self.run_launcher('--camera-only')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)['status'], 'conflict')

    def test_existing_empty_camera_waits_without_restart_or_output_directory(self):
        with listener(dict(ok=True, version=0, updated=0, bytes=2), {}) as self.camera_port:
            result = self.run_launcher('--camera-only', '--startup-timeout', '0.25')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)['status'], 'waiting')
            self.assertIn('left running', result.stderr.lower())
            self.assertFalse((self.bundle / 'runs').exists())

    def test_new_empty_camera_is_not_reported_ready_or_killed(self):
        result = self.run_launcher('--camera-only', '--startup-timeout', '0.25',
                                   env=dict(self.env, TEST_CAMERA_MODE='pending'))
        self.assertNotEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertEqual(data['status'], 'waiting')
        os.kill(data['services']['camera']['pid'], 0)

    def test_camera_exit_is_reported_without_starting_observer(self):
        result = self.run_launcher(env=dict(self.env, TEST_CAMERA_MODE='exit'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('exited', result.stderr.lower())
        self.assertFalse(list(self.bundle.rglob('observer.pid')))

    def test_observer_launcher_exit_reports_error_and_keeps_started_camera(self):
        (self.bundle / 'start_observer.sh').write_text('exit 9\n')
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('exited', result.stderr.lower())
        data = json.loads(result.stdout)
        self.assertEqual(data['status'], 'error')
        os.kill(data['services']['camera']['pid'], 0)
        self.assertFalse(list(self.bundle.rglob('observer.pid')))

    def test_empty_image_values_do_not_make_existing_camera_ready(self):
        snapshot = dict(schema_version=1, robot_id='R1Pro2026_001', observed_at=1,
                        images=dict(head='', wrist_left='', wrist_right=''))
        with listener(dict(ok=True, version=1, updated=1, bytes=99), snapshot) as self.camera_port:
            result = self.run_launcher('--camera-only', '--startup-timeout', '0.25')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)['status'], 'waiting')
            self.assertFalse((self.bundle / 'runs').exists())

    def test_missing_legacy_source_does_not_start_camera_or_create_source(self):
        missing = self.base / 'missing-original'
        result = self.run_launcher(env=dict(self.env, VLA_LEGACY_ROOT=str(missing)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('readable existing', result.stderr)
        self.assertFalse(missing.exists())
        self.assertFalse((self.bundle / 'runs').exists())

    def test_concurrent_launches_start_one_camera_and_one_observer(self):
        processes = [subprocess.Popen(self.command(), cwd=self.base, env=self.env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(2)]
        reports = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=12)
            self.assertEqual(process.returncode, 0, stderr)
            reports.append(json.loads(stdout))
        self.assertEqual(len(list(self.bundle.rglob('started.json'))), 1)
        self.assertEqual(len(list(self.bundle.rglob('observer.pid'))), 1)
        self.assertEqual({r['services']['camera']['status'] for r in reports}, {'started', 'reused'})
        self.assertEqual(len({r['services']['vla']['pid'] for r in reports}), 1)

    def test_output_root_inside_or_above_original_logs_is_rejected_before_writes(self):
        for root in (self.legacy, self.legacy / 'new', self.original):
            with self.subTest(root=root):
                result = self.run_launcher('--root', str(root))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('separate', result.stderr.lower())
                self.assertEqual(fingerprint(self.original), self.original_before)
                self.assertFalse((root / '.start.lock').exists())

    def test_symlinked_runs_cannot_escape_root(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (self.bundle / 'runs').symlink_to(outside, target_is_directory=True)
        result = self.run_launcher('--camera-only')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
