"""One-file launch defaults, explicit overrides, and real local service cleanup."""
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
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'exhibit'))


class LiveEntryTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'exhibit/live_entry.py').is_file(),
                        'One-click entry must apply saved settings before launch')
        import live_entry
        self.entry = live_entry
        self.temp = tempfile.TemporaryDirectory(prefix='r1-entry-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / 'settings.json'
        self.config = dict(robot_ip='127.0.0.1', robot_user='nvidia', ssh_port=22,
            port=8773, robot_clock_offset='auto',
            snapshot_file=str(self.root / 'data/snapshot.json'), db=str(self.root / 'data/fleet.sqlite3'))
        self.settings.write_text(json.dumps(self.config))

    def prepare(self, argv=()):
        return self.entry.prepare(list(argv), self.settings)

    def test_saved_paths_and_measured_offset_are_passed_to_real_launcher_parser(self):
        import live_launcher
        with patch.object(self.entry, 'measure_robot_clock', return_value={'offset_s': 13.55, 'rtt_s': .06}):
            args = live_launcher.build_parser().parse_args(self.prepare())
        self.assertEqual(args.robot_ip, '127.0.0.1')
        self.assertEqual(args.port, 8773)
        self.assertEqual(args.robot_clock_offset, 13.55)
        self.assertEqual(args.snapshot_file, str(self.root / 'data/snapshot.json'))
        self.assertEqual(args.db, str(self.root / 'data/fleet.sqlite3'))

    def test_explicit_options_override_saved_settings_without_ssh(self):
        import live_launcher
        with patch.object(self.entry, 'measure_robot_clock', side_effect=AssertionError('must not SSH')):
            args = live_launcher.build_parser().parse_args(self.prepare([
                '--robot-ip', '127.0.0.2', '--port', '8780', '--robot-clock-offset', '-2', '--no-ssh']))
        self.assertEqual((args.robot_ip, args.port, args.robot_clock_offset), ('127.0.0.2', 8780, -2))
        self.assertTrue(args.no_ssh)

    def test_probe_failure_does_not_fall_back_to_yesterdays_offset(self):
        with patch.object(self.entry, 'measure_robot_clock', side_effect=self.entry.ClockProbeError('unreachable')):
            with self.assertRaises(self.entry.EntryError):
                self.prepare()

    def test_saved_services_script_is_forwarded_with_explicit_offset(self):
        import live_launcher
        self.config['services_script'] = '/robot/new folder/start_services.py'
        self.settings.write_text(json.dumps(self.config))
        with patch.object(self.entry, 'measure_robot_clock', side_effect=AssertionError('must not SSH')):
            args = live_launcher.build_parser().parse_args(self.prepare(['--robot-clock-offset', '0']))
        self.assertEqual(args.services_script, '/robot/new folder/start_services.py')

    def test_no_ssh_requires_explicit_offset_but_check_and_help_never_probe(self):
        import live_launcher
        with patch.object(self.entry, 'measure_robot_clock', side_effect=AssertionError('must not SSH')):
            with self.assertRaises(self.entry.EntryError):
                self.prepare(['--no-ssh'])
            args = live_launcher.build_parser().parse_args(self.prepare(['--check']))
            self.assertTrue(args.check)
            self.assertEqual(args.robot_clock_offset, 0)
            with self.assertRaises(SystemExit) as caught:
                self.prepare(['--help'])
            self.assertEqual(caught.exception.code, 0)

    def test_invalid_settings_and_options_fail_before_network(self):
        with patch.object(self.entry, 'measure_robot_clock', side_effect=AssertionError('must not SSH')):
            for extra in [{'password': 'must-not-be-stored'}, {'port': True}, {'robot_clock_offset': float('nan')}, {'snapshot_file': ''}]:
                self.settings.write_text(json.dumps(dict(self.config, **extra)))
                with self.subTest(extra=extra), self.assertRaises(self.entry.EntryError):
                    self.prepare()
            self.settings.write_text(json.dumps(self.config))
            for args in [['--robot-ip', 'bad'], ['--port', '0'], ['--ssh-port', '0'], ['--wait-seconds', 'nan']]:
                with self.subTest(args=args), self.assertRaises((self.entry.EntryError, SystemExit)):
                    self.prepare(args)

    def test_help_is_available_with_missing_or_invalid_settings(self):
        for content in (None, '{broken json'):
            if content is None:
                self.settings.unlink()
            else:
                self.settings.write_text(content)
            with self.subTest(content=content):
                try:
                    self.prepare(['--help'])
                except BaseException as exc:
                    self.assertIsInstance(exc, SystemExit)
                    self.assertEqual(exc.code, 0)
                else:
                    self.fail('help must exit before starting anything')

    def test_terminating_during_clock_probe_reaps_owned_ssh(self):
        fake = self.root/'ssh'
        child_pid = self.root/'ssh.pid'
        fake.write_text('#!'+sys.executable+'\nimport os,time\nfrom pathlib import Path\nPath('+repr(str(child_pid))+').write_text(str(os.getpid()))\ntime.sleep(30)\n')
        fake.chmod(0o755)
        env=dict(os.environ,R1_LIVE_SETTINGS=str(self.settings),PATH=str(self.root)+os.pathsep+os.environ['PATH'],PYTHONDONTWRITEBYTECODE='1')
        proc=subprocess.Popen([str(ROOT/'start_live.sh')],cwd=self.root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        pid=None
        try:
            deadline=time.monotonic()+4
            while not child_pid.exists() and proc.poll() is None and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertTrue(child_pid.exists(), 'clock probe was not started')
            pid=int(child_pid.read_text())
            proc.terminate();proc.wait(timeout=4)
            state=Path('/proc/%d/stat'%pid)
            self.assertFalse(state.exists(), 'owned SSH child remains after terminating one-click startup')
        finally:
            if proc.poll() is None:proc.kill();proc.wait(timeout=2)
            if pid is not None and Path('/proc/%d'%pid).exists():
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass

    def test_shell_entry_starts_real_server_from_saved_config_and_stops_children(self):
        # A real local camera plus a fake SSH executable exercises the full shell,
        # probe, receiver, and server chain without reaching a robot or browser.
        test = self
        class Camera(BaseHTTPRequestHandler):
            def do_GET(self):
                pixel = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()
                raw = json.dumps(dict(schema_version=1, robot_id='entry-fixture', run_id='camera',
                    observed_at=time.time()+10-.5, task='fixture', model_id='fixture', stage='camera',
                    rtt_ms=123, images={k:pixel for k in ('head','wrist_left','wrist_right')})).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def log_message(self,*_): pass
        camera = ThreadingHTTPServer(('127.0.0.1',0),Camera)
        thread = threading.Thread(target=camera.serve_forever,daemon=True);thread.start()
        self.addCleanup(camera.server_close);self.addCleanup(camera.shutdown)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.config.update(port=port,robot_port=camera.server_port)
        self.settings.write_text(json.dumps(self.config))
        fake = self.root/'ssh'
        fake.write_text('#!'+sys.executable+'\nimport sys,time\nprint("READY",flush=True)\nfor line in sys.stdin:\n print(time.time()+10,flush=True)\n')
        fake.chmod(0o755)
        env=dict(os.environ,R1_LIVE_SETTINGS=str(self.settings),PATH=str(self.root)+os.pathsep+os.environ['PATH'],PYTHONDONTWRITEBYTECODE='1')
        with (self.root/'launch.log').open('w+') as log:
            proc=subprocess.Popen([str(ROOT/'start_live.sh'),'--no-browser','--no-vla'],cwd=self.root,env=env,stdout=log,stderr=log)
            try:
                opener=build_opener(ProxyHandler({}));deadline=time.monotonic()+8;state=None
                while proc.poll() is None and time.monotonic()<deadline:
                    try:
                        with opener.open('http://127.0.0.1:%d/api/telemetry'%port,timeout=.3) as response:state=json.load(response)
                        if state.get('connected'):break
                    except OSError:pass
                    time.sleep(.05)
                log.flush();log.seek(0);output=log.read()
                self.assertIsNotNone(state,output)
                self.assertTrue(state['connected'],state)
                self.assertAlmostEqual(state['robot_clock_offset_s'],10,delta=.15)
                self.assertEqual(state['state']['rtt_ms'],123)
                self.assertTrue((self.root/'data/fleet.sqlite3').exists())
                proc.send_signal(signal.SIGINT);proc.wait(timeout=6)
                self.assertEqual(proc.returncode,0)
                with socket.socket() as sock:self.assertNotEqual(sock.connect_ex(('127.0.0.1',port)),0)
            finally:
                if proc.poll() is None:proc.terminate();proc.wait(timeout=6)


if __name__ == '__main__':
    unittest.main()
