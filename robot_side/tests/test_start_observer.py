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
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

SCRIPT=Path(__file__).resolve().parents[1]/'start_observer.sh'

class ObserverStartTests(unittest.TestCase):
    def test_start_reuse_and_health_without_control_commands(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);bundle=root/'observer';bundle.mkdir();source=root/'original_runtime';source.mkdir()
            (source/'keep.txt').write_text('original contents')
            for name in ('start_observer.sh','bridge.py'):shutil.copyfile(SCRIPT.parent/name,bundle/name)
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            env=dict(os.environ,VLA_PYTHON=sys.executable,VLA_OBSERVER_PORT=str(port),VLA_LEGACY_ROOT=str(source))
            proc=None
            try:
                first=subprocess.run(['bash',str(bundle/'start_observer.sh')],env=env,capture_output=True,text=True,timeout=10)
                self.assertEqual(first.returncode,0,first.stderr);data=json.loads(first.stdout)
                self.assertEqual(data['status'],'started');pid=data['pid']
                self.assertEqual((bundle/'runtime/observer.pid').read_text().strip(),str(pid))
                again=subprocess.run(['bash',str(bundle/'start_observer.sh')],env=env,capture_output=True,text=True,timeout=10)
                self.assertEqual(again.returncode,0,again.stderr);reuse=json.loads(again.stdout)
                self.assertEqual(reuse['status'],'reused');self.assertEqual(reuse['pid'],pid)
                self.assertEqual([p.name for p in source.iterdir()],['keep.txt'])
                self.assertEqual((source/'keep.txt').read_text(),'original contents')
                self.assertFalse((root/'runtime').exists())
            finally:
                pidfile=bundle/'runtime/observer.pid'
                if pidfile.exists():
                    try:os.kill(int(pidfile.read_text()),signal.SIGTERM)
                    except ProcessLookupError:pass
    def test_unrelated_listener_is_conflict_and_is_left_running(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):self.send_response(200);self.end_headers();self.wfile.write(b'{"unrelated":true}')
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as d:
                source=Path(d)/'source';source.mkdir()
                env=dict(os.environ,VLA_PYTHON=sys.executable,VLA_OBSERVER_ROOT=str(Path(d)/'observer'),VLA_OBSERVER_PORT=str(server.server_port),VLA_LEGACY_ROOT=str(source))
                result=subprocess.run(['bash',str(SCRIPT)],env=env,capture_output=True,text=True,timeout=10)
                self.assertNotEqual(result.returncode,0);self.assertIn('conflict',result.stderr)
                with socket.create_connection(('127.0.0.1',server.server_port),timeout=1):pass
        finally:server.shutdown();server.server_close();thread.join()

    def test_missing_source_refuses_start_without_creating_original_or_runtime(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'observer';source=Path(d)/'missing_original_runtime'
            env=dict(os.environ,VLA_PYTHON=sys.executable,VLA_OBSERVER_ROOT=str(root),VLA_LEGACY_ROOT=str(source))
            result=subprocess.run(['bash',str(SCRIPT)],env=env,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('readable existing',result.stderr)
            self.assertFalse(source.exists());self.assertFalse(root.exists())
