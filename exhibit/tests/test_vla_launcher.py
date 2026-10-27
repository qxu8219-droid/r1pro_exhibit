from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher
import live_launcher
from instance_identity import instance_identity
from test_vla_receiver import source_http


class VlaLauncherTests(unittest.TestCase):
    def test_observer_ssh_command_is_read_only_quoted_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder); log=folder/'args.json'; ssh=folder/'ssh'
            ssh.write_text('#!/usr/bin/python3\nimport json,os,sys\nopen(os.environ["VLA_TEST_ARGS"],"w").write(json.dumps(sys.argv[1:]))\nsys.exit(int(os.environ.get("VLA_TEST_EXIT","0")))\n')
            ssh.chmod(0o755)
            with patch.dict(os.environ,{'PATH':str(folder),'VLA_TEST_ARGS':str(log)}):
                live_launcher.start_remote_observer('127.0.0.1',script="/tmp/space ' quote/start.sh",port=8884)
                args=json.loads(log.read_text())
                self.assertIn('StrictHostKeyChecking=ask',args)
                self.assertEqual(args[-2],'nvidia@127.0.0.1')
                import shlex
                self.assertEqual(shlex.split(args[-1]),['VLA_OBSERVER_PORT=8884','bash',"/tmp/space ' quote/start.sh"])
                with patch.dict(os.environ,{'VLA_TEST_EXIT':'7'}), self.assertRaises(live_launcher.LiveError):
                    live_launcher.start_remote_observer('127.0.0.1')
                with self.assertRaises(live_launcher.LiveError):
                    live_launcher.start_remote_observer('localhost; echo unsafe')

    def test_live_launch_receives_vla_without_changing_camera_snapshot(self):
        image = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPVcAAAAASUVORK5CYII='
        class Camera(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_GET(self):
                raw = json.dumps(dict(schema_version=1, robot_id='robot239', run_id='camera-session',
                    observed_at=time.time(), task='现场相机', model_id='未使用动作模型', stage='相机回传',
                    images={k:image for k in ('head','wrist_left','wrist_right')})).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
        camera = ThreadingHTTPServer(('127.0.0.1', 0), Camera)
        worker = threading.Thread(target=camera.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as folder, source_http() as (source, state):
                folder = Path(folder)
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
                def browser(url, browser_port):
                    opener = launcher.build_opener(launcher.ProxyHandler({}))
                    deadline = time.monotonic()+4
                    while time.monotonic()<deadline:
                        with opener.open('http://127.0.0.1:%d/api/vla/state' % port, timeout=1) as response:
                            evidence = json.load(response)
                        if (evidence.get('state') or {}).get('run_id') == 'run-a': break
                        time.sleep(.03)
                    self.assertEqual(evidence['state']['run_id'], 'run-a')
                    self.assertEqual(json.loads((folder/'snapshot.json').read_text())['run_id'], 'camera-session')
                    raise KeyboardInterrupt
                with patch.object(live_launcher,'open_browser',side_effect=browser):
                    result = live_launcher.main(['--robot-ip','127.0.0.1','--robot-port',str(camera.server_port),
                        '--vla-port',source.rsplit(':',1)[1],'--no-ssh','--port',str(port),
                        '--db',str(folder/'sim.db'),'--snapshot-file',str(folder/'snapshot.json')])
                self.assertEqual(result,0)
                self.assertTrue((folder/'vla.sqlite3').exists())
        finally:
            camera.shutdown(); camera.server_close(); worker.join(2)

    def test_vla_database_changes_identity_while_absent_keeps_old_identity(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(instance_identity(root, "sim.db", 4),
                         instance_identity(root, "sim.db", 4, vla_db=None))
        self.assertNotEqual(instance_identity(root, "sim.db", 4),
                            instance_identity(root, "sim.db", 4, vla_db="vla.db"))

    def test_server_with_different_vla_database_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            db = folder / "sim.db"
            vla = folder / "vla.db"
            process = launcher.ensure_server(port, db, vla_db=vla)
            try:
                self.assertEqual(launcher.probe(port, db, vla_db=vla), "exhibit")
                self.assertEqual(launcher.probe(port, db), "incompatible")
                self.assertEqual(launcher.probe(port, db, vla_db=folder / "other.db"), "incompatible")
                self.assertIsNone(launcher.ensure_server(port, db, vla_db=vla))
            finally:
                launcher.stop_owned_server(process)


if __name__ == "__main__":
    unittest.main()
