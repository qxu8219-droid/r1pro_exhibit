"""Remote startup contract using loopback sockets and a disposable SSH executable."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_launcher as live


class RemoteBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='R1 remote startup ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.ssh_log = self.root / 'ssh-argv.json'
        self.shell_log = self.root / 'bash-argv.json'
        self.curl_log = self.root / 'curl-argv.json'
        self.write_executable('ssh', """import json, os, sys
with open(os.environ['SSH_TEST_LOG'], 'w') as stream:
    json.dump(sys.argv[1:], stream)
sys.exit(int(os.environ.get('SSH_TEST_STATUS', '0')))
""")
        self.env = dict(os.environ, PATH=str(self.bin), SSH_TEST_LOG=str(self.ssh_log),
                        SSH_TEST_STATUS='0', SHELL_TEST_LOG=str(self.shell_log), CURL_TEST_LOG=str(self.curl_log))

    def write_executable(self, name, source):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + source)
        path.chmod(0o755)

    def require_helper(self, name):
        helper = getattr(live, name, None)
        self.assertTrue(callable(helper), name + ' is required for remote startup')
        return helper

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def args(self, robot_port, *extra):
        return ['--robot-ip', '127.0.0.1', '--robot-port', str(robot_port),
                '--port', str(self.free_port()), '--snapshot-file', str(self.root/'snapshot.json'),
                '--db', str(self.root/'fleet.sqlite3'), '--no-browser', '--no-vla', '--wait-seconds', '0', *extra]

    def camera(self, body=None, status=200, port=0):
        class Camera(BaseHTTPRequestHandler):
            def do_GET(self):
                image = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()
                raw = body if body is not None else json.dumps(dict(
                    schema_version=1, robot_id='qa-remote', run_id='remote-started',
                    task='camera test', model_id='no model', stage='camera', observed_at=time.time(),
                    images={key:image for key in ('head', 'wrist_left', 'wrist_right')})).encode()
                self.send_response(status)
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', port), Camera)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(stop)
        return server.server_port

    def test_tcp_probe_distinguishes_listening_and_closed_ports(self):
        probe = self.require_helper('bridge_reachable')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            listener.listen()
            self.assertTrue(probe('127.0.0.1', port, timeout=.2))
        self.assertFalse(probe('127.0.0.1', port, timeout=.2))

    def test_ssh_process_receives_custom_connection_and_shell_safe_script(self):
        start = self.require_helper('start_remote_bridge')
        marker = self.root/'unexpected-command'
        script = "/robot/camera bridge'$(touch " + str(marker) + ")/start.sh"
        with patch.dict(os.environ, self.env, clear=True):
            self.assertIsNone(start('192.0.2.73', robot_user='qa-user', ssh_port=2207, bridge_script=script))
        argv = json.loads(self.ssh_log.read_text())
        self.assertIn('qa-user@192.0.2.73', argv)
        self.assertEqual(argv[argv.index('-p')+1], '2207')
        self.assertFalse(any('StrictHostKeyChecking=no' in arg or 'UserKnownHostsFile=/dev/null' in arg for arg in argv))
        self.assertFalse(any('password' in arg.lower() or 'sshpass' in arg.lower() for arg in argv))
        command = argv[-1]
        self.write_executable('curl', "import json, os, sys\nwith open(os.environ['CURL_TEST_LOG'], 'w') as stream:\n    json.dump(sys.argv[1:], stream)\nsys.exit(int(os.environ['CURL_TEST_STATUS']))\n")
        self.write_executable('bash', "import json, os, sys\nwith open(os.environ['SHELL_TEST_LOG'], 'w') as stream:\n    json.dump(sys.argv[1:], stream)\n")
        self.write_executable('python3', "import os,sys\nsys.exit(int(os.environ['TCP_TEST_STATUS']))\n")
        for healthy, listening in ((True, True), (False, True), (False, False)):
            with self.subTest(healthy=healthy, listening=listening):
                self.shell_log.unlink(missing_ok=True)
                result = subprocess.run(['/bin/sh', '-c', command], env=dict(self.env, CURL_TEST_STATUS='0' if healthy else '22', TCP_TEST_STATUS='0' if listening else '1'), capture_output=True, timeout=3)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(marker.exists(), 'script path must remain inert shell data')
                curl_args = json.loads(self.curl_log.read_text())
                self.assertIn('http://127.0.0.1:8772/healthz', curl_args)
                self.assertEqual(curl_args[curl_args.index('--max-time') + 1], '2')
                self.assertIn('-fsS', curl_args)
                self.assertEqual(self.shell_log.exists(), not healthy and not listening, 'listening bridge must be reused even when health response fails')
                if not healthy and not listening:
                    self.assertEqual(json.loads(self.shell_log.read_text()), [script])

    def test_ssh_failure_and_missing_client_are_actionable_live_errors(self):
        start = self.require_helper('start_remote_bridge')
        with patch.dict(os.environ, dict(self.env, SSH_TEST_STATUS='255'), clear=True):
            with self.assertRaises(live.LiveError):
                start('192.0.2.73')
        self.assertTrue(self.ssh_log.exists())
        (self.bin/'ssh').unlink()
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(live.LiveError):
                start('192.0.2.73')

    def test_check_bad_snapshot_is_read_only_and_never_invokes_ssh(self):
        port = self.camera(body=b'{}')
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(live.main(self.args(port, '--check')), 1)
        self.assertFalse(self.ssh_log.exists())
        self.assertFalse((self.root/'snapshot.json').exists())
        self.assertFalse((self.root/'fleet.sqlite3').exists())
        self.assertFalse(list(self.root.glob('*.lock')))

    def test_check_closed_port_never_invokes_ssh(self):
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(live.main(self.args(self.free_port(), '--check')), 1)
        self.assertFalse(self.ssh_log.exists())
        self.assertFalse((self.root/'snapshot.json').exists())

    def test_reachable_but_empty_or_bad_http_bridge_is_never_restarted(self):
        self.require_helper('start_remote_bridge')
        for body, status in ((b'{}', 200), (b'broken', 200), (b'{}', 503)):
            with self.subTest(body=body, status=status):
                port = self.camera(body=body, status=status)
                with patch.dict(os.environ, self.env, clear=True):
                    self.assertEqual(live.main(self.args(port)), 1)
                self.assertFalse(self.ssh_log.exists())
                self.assertFalse((self.root/'snapshot.json').exists())

    def test_disabled_ssh_leaves_absent_bridge_and_snapshot_untouched(self):
        self.require_helper('start_remote_bridge')
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(live.main(self.args(self.free_port(), '--no-ssh')), 1)
        self.assertFalse(self.ssh_log.exists())
        self.assertFalse((self.root/'snapshot.json').exists())

    def test_missing_camera_never_runs_original_robot_script(self):
        self.require_helper('start_remote_bridge')
        self.write_executable('ssh', """import os, sys
with open(os.environ['SSH_TEST_LOG'], 'a') as stream:
    stream.write('attempt\\n')
sys.exit(0)
""")
        args = self.args(self.free_port())
        args[args.index('--wait-seconds') + 1] = '1.1'
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(live.main(args), 1)
        self.assertFalse(self.ssh_log.exists())
        self.assertFalse((self.root/'snapshot.json').exists())

    def test_cancelled_camera_read_exits_cleanly_without_snapshot(self):
        self.require_helper('start_remote_bridge')
        with patch.object(live, 'fetch_snapshot', side_effect=KeyboardInterrupt):
            self.assertEqual(live.main(self.args(self.free_port())), 0)
        self.assertFalse((self.root/'snapshot.json').exists())

    def test_existing_camera_is_read_without_running_original_script(self):
        self.require_helper('start_remote_bridge')
        robot_port = self.camera()
        args = self.args(robot_port, '--robot-user', 'qa-user', '--ssh-port', '2207',
                         '--bridge-script', '/robot/camera/start.sh')
        args[args.index('--wait-seconds') + 1] = '2'
        args.remove('--no-browser')
        calls = []
        observed = []
        def start(robot_ip, robot_user='nvidia', ssh_port=22,
                  bridge_script='/home/nvidia/r1pro_ws/vision_event_capture/live_stream/start_bridge.sh'):
            calls.append((robot_ip, robot_user, ssh_port, bridge_script))
            self.camera(port=robot_port)
        def browser(url, port):
            with build_opener(ProxyHandler({})).open('http://127.0.0.1:%d/api/telemetry' % port, timeout=2) as response:
                observed.append(json.load(response))
            raise KeyboardInterrupt
        with patch.object(live, 'start_remote_bridge', side_effect=start), patch.object(live, 'open_browser', side_effect=browser):
            self.assertEqual(live.main(args), 0)
        self.assertEqual(calls, [])
        self.assertEqual(json.loads((self.root/'snapshot.json').read_bytes())['run_id'], 'remote-started')
        self.assertTrue(observed[0]['connected'])
        self.assertEqual(observed[0]['state']['run_id'], 'remote-started')


if __name__ == '__main__':
    unittest.main()
