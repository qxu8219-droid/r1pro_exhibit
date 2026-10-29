"""Full-demo helper tests use local fake model, SSH and controller programs only."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / 'full_session.py'


def load_module():
    if not SOURCE.exists():
        return None
    spec = importlib.util.spec_from_file_location('full_session', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


session = load_module()


class FullSessionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(session, 'full_session.py must implement the isolated full-demo helper')
        for name in ('prepare', 'release', 'probe_model'):
            self.assertTrue(callable(getattr(session, name, None)), name + ' API is missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'full-demo'
        self.root.mkdir()
        self.original = self.base / 'coordinator'
        self.original.mkdir()
        (self.original / 'start.sh').write_text('exit 0\n')
        (self.original / 'robot239_config.json').write_text('{}')
        self.services = self.base / 'start_services.py'
        self.services.write_text('print(\'{"status":"ready","services":{}}\')\n')
        self.config = dict(coordinator_dir=str(self.original),
                           robot_config=str(self.original / 'robot239_config.json'),
                           vla_python=sys.executable, services_script=str(self.services),
                           tunnel_host='100.105.13.29', tunnel_user='ksadmin',
                           tunnel_local_port=18128, tunnel_remote_port=18118)

    def test_process_matching_requires_real_python_entrypoint(self):
        # A diagnostic mentioning module names must never be treated as motion.
        yes = [['python3', '-m', 'flexible_coordinator.launcher', '--enable-motion'],
               ['/venv/bin/python3', '-u', '-m', 'flexible_coordinator.executor'],
               ['python3', str(self.original / 'executor.py')]]
        no = [['python3', '-c', 'print("flexible_coordinator.launcher")'],
              ['python3', '-c', 'x', '-m', 'flexible_coordinator.executor'],
              ['grep', 'flexible_coordinator.launcher'],
              ['python3', '/other/executor.py'],
              ['python3', '-m', 'other', 'flexible_coordinator.launcher']]
        for argv in yes:
            with self.subTest(argv=argv):
                self.assertTrue(session.is_controller(argv, self.config))
        for argv in no:
            with self.subTest(argv=argv):
                self.assertFalse(session.is_controller(argv, self.config))

    def test_check_never_creates_runs_or_starts_services(self):
        # Even a missing tunnel is a reportable read-only failure.
        before = list(self.base.rglob('*'))
        with mock.patch.object(session, 'controllers', return_value=[]), \
             mock.patch.object(session, 'occupied', return_value=False), \
             mock.patch.object(session.subprocess, 'Popen', side_effect=AssertionError('check launched a process')):
            report = session.check(self.root, self.config)
        self.assertFalse(report['ready'])
        self.assertEqual(report['model']['status'], 'unavailable')
        self.assertEqual(before, list(self.base.rglob('*')))

    def test_token_rejects_traversal_and_symlink(self):
        (self.root / 'runs').mkdir()
        for token in ('../outside', '/tmp/elsewhere', '.', 'x' * 32):
            with self.subTest(token=token), self.assertRaises(session.SessionError):
                session.load_run(self.root, token)
        token = 'a' * 32
        (self.root / 'runs' / token).symlink_to(self.original, target_is_directory=True)
        with self.assertRaises(session.SessionError):
            session.load_run(self.root, token)

    def make_ssh(self):
        binary = self.base / 'bin'
        binary.mkdir()
        script = binary / 'ssh'
        script.write_text('#!' + sys.executable + "\nimport json,os,sys\nwith open(os.environ['FAKE_SSH_LOG'],'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')\n")
        script.chmod(0o700)
        self.ssh_log = self.base / 'ssh.jsonl'
        return mock.patch.dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                               FAKE_SSH_LOG=str(self.ssh_log))

    def prepare(self, occupied=False):
        with mock.patch.object(session, 'controllers', return_value=[]), \
             mock.patch.object(session, 'occupied', return_value=occupied), \
             mock.patch.object(session, 'probe_model', return_value={'status':'ready','verified':True}):
            return session.prepare(self.root, self.config)

    def test_prepare_new_tunnel_uses_private_known_hosts_and_scoped_services(self):
        fake_home = self.base / 'user'
        (fake_home / '.ssh').mkdir(parents=True)
        original_hosts = fake_home / '.ssh' / 'known_hosts'
        original_hosts.write_text('existing-known-host\n')
        with self.make_ssh(), mock.patch.object(Path, 'home', return_value=fake_home):
            prepared = self.prepare()
        run = self.root / 'runs' / prepared['token']
        self.assertTrue(prepared['model']['verified'])
        self.assertTrue(prepared['services_ready'])
        self.assertTrue(prepared['tunnel']['owned'])
        argv = json.loads(self.ssh_log.read_text().splitlines()[0])
        for flag in ('-N', '-f', '-M', '-S'):
            self.assertIn(flag, argv)
        self.assertIn('StrictHostKeyChecking=ask', argv)
        self.assertIn('ExitOnForwardFailure=yes', argv)
        self.assertIn('UserKnownHostsFile=' + str(run / 'known_hosts'), argv)
        self.assertIn('127.0.0.1:18128:127.0.0.1:18118', argv)
        self.assertEqual(original_hosts.read_text(), 'existing-known-host\n')
        self.assertEqual((run / 'known_hosts').read_text(), 'existing-known-host\n')
        self.assertEqual(prepared['existing_controller'], [])
        self.assertTrue((run / 'config.json').is_file())
        self.assertEqual(json.loads((run / 'services.json').read_text())['status'], 'ready')

    def test_existing_listener_with_bad_hello_never_starts_tunnel_or_services(self):
        with self.make_ssh(), mock.patch.object(session, 'controllers', return_value=[]), \
             mock.patch.object(session, 'occupied', return_value=True), \
             mock.patch.object(session, 'probe_model', side_effect=session.SessionError('wrong model')):
            with self.assertRaisesRegex(session.SessionError, 'wrong model'):
                session.prepare(self.root, self.config)
        self.assertFalse(self.ssh_log.exists())
        self.assertEqual(list(self.root.rglob('services.json')), [])

    def test_release_never_closes_reused_tunnel_or_owned_tunnel_with_controller(self):
        with self.make_ssh():
            reused = self.prepare(occupied=True)
            with mock.patch.object(session, 'controllers', return_value=[]):
                result = session.release(self.root, reused['token'])
            self.assertFalse(result['tunnel_closed'])
            self.assertFalse(self.ssh_log.exists())
            own = self.prepare()
            with mock.patch.object(session, 'controllers', return_value=[123]):
                result = session.release(self.root, own['token'])
            self.assertFalse(result['tunnel_closed'])
            self.assertEqual(len(self.ssh_log.read_text().splitlines()), 1)
            with mock.patch.object(session, 'controllers', return_value=[]):
                result = session.release(self.root, own['token'])
            self.assertTrue(result['tunnel_closed'])
        close = json.loads(self.ssh_log.read_text().splitlines()[-1])
        self.assertEqual(close[close.index('-O') + 1], 'exit')
        self.assertIn(str(self.root / 'runs' / own['token'] / 'ssh.sock'), close)

    def test_prepare_service_failure_closes_only_own_tunnel(self):
        self.services.write_text('raise SystemExit(8)\n')
        with self.make_ssh(), mock.patch.object(session, 'controllers', return_value=[]), \
             mock.patch.object(session, 'occupied', return_value=False), \
             mock.patch.object(session, 'probe_model', return_value={'status':'ready','verified':True}):
            with self.assertRaises(session.SessionError):
                session.prepare(self.root, self.config)
        commands = [json.loads(line) for line in self.ssh_log.read_text().splitlines()]
        self.assertEqual(len(commands), 2)
        self.assertIn('exit', commands[-1])

    def fake_model_bundle(self, overrides=None):
        bundle = self.original / 'client_bundle'
        runtime = bundle / 'camera_runtime'
        (runtime / 'websockets' / 'sync').mkdir(parents=True)
        for path in (runtime / 'websockets' / '__init__.py', runtime / 'websockets' / 'sync' / '__init__.py'):
            path.write_text('')
        (runtime / 'g05_msgpack.py').write_text('import json\ndef unpackb(value):return json.loads(value)\n')
        (runtime / 'websockets' / 'sync' / 'client.py').write_text(
            'import json,os\nfrom pathlib import Path\n'
            'class Connection:\n'
            ' def __enter__(self):return self\n'
            ' def __exit__(self,*args):pass\n'
            ' def recv(self,timeout):return Path(os.environ["FAKE_MODEL_HELLO"]).read_bytes()\n'
            ' def send(self,*args):raise RuntimeError("Read-only hello sent inference/reset")\n'
            'def connect(url,**kwargs):\n'
            ' assert url.startswith("ws://127.0.0.1:")\n'
            ' assert os.environ.get("PYTHONDONTWRITEBYTECODE")=="1"\n'
            ' return Connection()\n')
        manifest = dict(protocol='b041-full23-chunk-v1', model_id='fixture-model',
                        checkpoint_sha256='a'*64,horizon=32,fps=15,mode='subtask_cot',
                        server_optimization_id='smooth_cot_inference_v1',fm_steps=10)
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
        robot = dict(client_bundle='client_bundle', manifest='manifest.json', model='R1')
        Path(self.config['robot_config']).write_text(json.dumps(robot))
        hello = {key:value for key,value in manifest.items() if key != 'fm_steps'}
        hello.update(overrides or {})
        path = self.base / 'hello.json'
        path.write_text(json.dumps(hello))
        return mock.patch.dict(os.environ, FAKE_MODEL_HELLO=str(path))

    def test_model_hello_verifies_manifest_without_inference_or_reset(self):
        with self.fake_model_bundle():
            before = sorted(str(p) for p in self.original.rglob('*'))
            result = session.probe_model(self.config)
        self.assertTrue(result['verified'])
        self.assertEqual(result['model_id'], 'fixture-model')
        self.assertEqual(before, sorted(str(p) for p in self.original.rglob('*')))

    def test_model_mismatch_and_missing_core_fields_are_rejected(self):
        with self.fake_model_bundle({'checkpoint_sha256':'wrong'}):
            with self.assertRaisesRegex(session.SessionError, 'checkpoint_sha256'):
                session.probe_model(self.config)
        with mock.patch.dict(os.environ, FAKE_MODEL_HELLO=str(self.base / 'hello.json')):
            hello = json.loads((self.base / 'hello.json').read_text())
            hello.pop('model_id')
            (self.base / 'hello.json').write_text(json.dumps(hello))
            with self.assertRaises(session.SessionError):
                session.probe_model(self.config)

    def prepare_control(self, body, send_heartbeat=True, port_alive=True, lease=.5):
        self.assertTrue(callable(getattr(session, 'run_session', None)), 'run_session API is missing')
        prepared = self.prepare(occupied=True)
        run = self.root / 'runs' / prepared['token']
        shutil.copyfile(SOURCE, self.root / 'full_session.py')
        (self.original / 'control.py').write_text(body)
        (self.original / 'start.sh').write_text('exec "$VLA_PYTHON" -B control.py "$@"\n')
        driver = ('import sys;sys.path.insert(0,sys.argv[1]);import full_session as m;'
                  'm.controllers=lambda config: [];m.occupied=lambda port:' + repr(port_alive) + ';m.HEARTBEAT_TIMEOUT=' + repr(lease) + ';'
                  'm.probe_model=lambda config: {"status":"ready","verified":True};'
                  'raise SystemExit(m.main(["--run",sys.argv[2]]))')
        env = dict(os.environ, FAKE_CONTROL_RECORD=str(run / 'fixture.json'), PYTHONDONTWRITEBYTECODE='1')
        process = subprocess.Popen([sys.executable, '-B', '-c', driver, str(self.root), prepared['token']],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        armed = process.stdout.readline()
        self.assertTrue(armed.startswith('R1_FULL_ARMED '), armed)
        if send_heartbeat:
            process.stdin.write('R1_FULL_HEARTBEAT\n')
            process.stdin.flush()
        return process, run, prepared

    def test_run_forwards_signal_once_waits_for_stop_and_consumes_token(self):
        body = ("import json,os,signal,sys,time\nfrom pathlib import Path\n"
                "count=0\ndef stop(*args):\n global count;count+=1\n"
                "signal.signal(signal.SIGINT,stop)\n"
                "Path(os.environ['FAKE_CONTROL_RECORD']).write_text(json.dumps(dict(pid=os.getpid(),sid=os.getsid(0),cwd=os.getcwd(),argv=sys.argv,env={k:os.environ[k] for k in ('B041_POLICY_URL','VLA_PYTHON','ROBOT_CONFIG')})))\n"
                "print('motion started',flush=True)\n"
                "while not count:time.sleep(.01)\n"
                "time.sleep(.35)\nprint('signals='+str(count),flush=True)\nprint('Stop confirmed.',flush=True)\n")
        process, run, prepared = self.prepare_control(body)
        deadline = time.monotonic() + 5
        while not (run / 'fixture.json').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((run / 'fixture.json').exists())
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        time.sleep(.05)
        process.send_signal(signal.SIGHUP)
        stdout, stderr = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertGreaterEqual(time.monotonic() - started, .3)
        result = json.loads(stdout.split('R1_FULL_RESULT ')[-1])
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(result['stop_confirmed'])
        self.assertEqual(result['returncode'], 0)
        self.assertIn('signals=1', stdout)
        record = json.loads((run / 'fixture.json').read_text())
        self.assertEqual(record['pid'], record['sid'])
        self.assertEqual(record['cwd'], str(self.original))
        self.assertEqual(record['argv'][1:], ['--enable-motion'])
        self.assertEqual(record['env']['B041_POLICY_URL'], 'ws://127.0.0.1:18128')
        self.assertEqual(record['env']['ROBOT_CONFIG'], self.config['robot_config'])
        with mock.patch.object(session, 'controllers', return_value=[]):
            with self.assertRaisesRegex(session.SessionError, 'already|consumed|prepared'):
                session.run_session(self.root, prepared['token'])

    def test_child_failure_is_not_stop_confirmation(self):
        process, run, prepared = self.prepare_control("print('child failed',flush=True)\nraise SystemExit(7)\n")
        process.wait(timeout=5)
        stdout, stderr = process.communicate(timeout=8)
        result = json.loads(stdout.split('R1_FULL_RESULT ')[-1])
        self.assertEqual(result['returncode'], 7)
        self.assertFalse(result['stop_confirmed'])
        self.assertEqual(result['status'], 'failed')
        self.assertNotEqual(process.returncode, 0)

    def test_existing_controller_prevents_run_without_consuming_other_process(self):
        self.assertTrue(callable(getattr(session, 'run_session', None)), 'run_session API is missing')
        prepared = self.prepare(occupied=True)
        with mock.patch.object(session, 'controllers', return_value=[1234]), \
             mock.patch.object(session.subprocess, 'Popen', side_effect=AssertionError('duplicate motion')):
            result = session.run_session(self.root, prepared['token'])
        self.assertEqual(result['status'], 'existing_controller')
        self.assertFalse(result['owned'])
        self.assertFalse(result['stop_confirmed'])

    def test_no_first_heartbeat_never_starts_motion(self):
        process, run, prepared = self.prepare_control("raise RuntimeError('motion must not start')\n", send_heartbeat=False)
        time.sleep(.65)
        stdout, stderr = process.communicate(timeout=5)
        result = json.loads(stdout.split('R1_FULL_RESULT ')[-1])
        self.assertFalse((run / 'motion.log').exists())
        self.assertFalse(result['owned'])
        self.assertFalse(result['stop_confirmed'])
        self.assertEqual(result['cancel_reason'], 'heartbeat_timeout')

    def test_heartbeat_loss_requests_original_stop(self):
        body = ("import signal,time\nrequested=False\ndef stop(*args):\n global requested;requested=True\n"
                "signal.signal(signal.SIGINT,stop)\nprint('started',flush=True)\n"
                "while not requested:time.sleep(.01)\nprint('Stop confirmed.',flush=True)\n")
        process, run, prepared = self.prepare_control(body)
        time.sleep(.8)
        stdout, stderr = process.communicate(timeout=5)
        result = json.loads(stdout.split('R1_FULL_RESULT ')[-1])
        self.assertTrue(result['stop_confirmed'])
        self.assertEqual(result['cancel_reason'], 'heartbeat_timeout')

    def test_relative_original_executor_is_detected_only_in_original_directory(self):
        self.assertTrue(session.is_controller(['python3', 'executor.py'], self.config, self.original))
        self.assertFalse(session.is_controller(['python3', 'executor.py'], self.config, self.base))

    def test_existing_stop_marker_does_not_confirm_a_later_failed_cancellation(self):
        body = ("import signal,time\nrequested=False\ndef stop(*args):\n global requested;requested=True\n"
                "signal.signal(signal.SIGINT,stop)\nprint('Stop confirmed.',flush=True)\n"
                "print('next motion started',flush=True)\n"
                "while not requested:time.sleep(.01)\n")
        process, run, prepared = self.prepare_control(body)
        time.sleep(.8)
        stdout, stderr = process.communicate(timeout=5)
        result = json.loads(stdout.split('R1_FULL_RESULT ')[-1])
        self.assertEqual(result['returncode'], 0)
        self.assertFalse(result['stop_confirmed'])

    def test_release_is_blocked_until_original_stop_completes(self):
        body = ("import signal,time\nrequested=False\ndef stop(*args):\n global requested;requested=True\n"
                "signal.signal(signal.SIGINT,stop)\nprint('started',flush=True)\n"
                "while not requested:time.sleep(.01)\n"
                "time.sleep(.5)\nprint('Stop confirmed.',flush=True)\n")
        process, run, prepared = self.prepare_control(body)
        time.sleep(.15)
        process.send_signal(signal.SIGINT)
        with mock.patch.object(session, 'controllers', return_value=[]):
            result = session.release(self.root, prepared['token'])
        self.assertEqual(result['status'], 'motion_active')
        process.communicate(timeout=5)

    def test_broken_stdout_still_waits_for_original_cancel_and_saves_result(self):
        body = ("import signal,time\nrequested=False\ndef stop(*args):\n global requested;requested=True\n"
                "signal.signal(signal.SIGINT,stop)\n"
                "while not requested:print('motion log',flush=True);time.sleep(.02)\n"
                "time.sleep(.2)\nprint('Stop confirmed.',flush=True)\n")
        process, run, prepared = self.prepare_control(body)
        time.sleep(.1)
        process.stdout.close()
        process.wait(timeout=5)
        result = json.loads((run / 'result.json').read_text())
        self.assertTrue(result['stop_confirmed'])
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(result['cancel_reason'], 'output_disconnected')
        process.stdin.close()
        process.stderr.close()

    def test_signal_during_popen_is_forwarded_after_child_assignment_once(self):
        prepared = self.prepare(occupied=True)
        sent = []
        class Child:
            pid = 123456
            def poll(self): return None
            def send_signal(self, signum): sent.append(signum)
            def wait(self): return 0
        def launch(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            return Child()
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'R1_FULL_HEARTBEAT\n')
        try:
            with os.fdopen(read_fd) as source, \
                 mock.patch.object(session.sys, 'stdin', source), \
                 mock.patch.object(session.sys, 'stdout', io.StringIO()), \
                 mock.patch.object(session, 'controllers', return_value=[]), \
                 mock.patch.object(session, 'probe_model', return_value={'verified':True}), \
                 mock.patch.object(session.subprocess, 'Popen', side_effect=launch), \
                 mock.patch.object(session.time, 'sleep', side_effect=[None, RuntimeError('end fake supervisor loop')]):
                result = session.run_session(self.root, prepared['token'])
        finally:
            os.close(write_fd)
        self.assertEqual(sent, [signal.SIGINT])
        self.assertEqual(result['cancel_reason'], 'SIGTERM')
        self.assertFalse(result['stop_confirmed'])

    def test_tunnel_loss_requests_original_cancel(self):
        body = ("import signal,time\nrequested=False\ndef stop(*args):\n global requested;requested=True\n"
                "signal.signal(signal.SIGINT,stop)\nprint('started',flush=True)\n"
                "while not requested:time.sleep(.01)\nprint('Stop confirmed.',flush=True)\n")
        process, run, prepared = self.prepare_control(body, port_alive=False, lease=3)
        process.wait(timeout=5)
        stdout, stderr = process.communicate()
        result = json.loads(stdout.split('R1_FULL_RESULT ')[-1])
        self.assertEqual(result['cancel_reason'], 'model_tunnel_lost')
        self.assertTrue(result['stop_confirmed'])

    def test_cli_check_is_read_only_and_does_not_invoke_original_check(self):
        shutil.copyfile(SOURCE, self.root / 'full_session.py')
        self.original.joinpath('start.sh').write_text('touch original-was-executed\n')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.config['tunnel_local_port'] = sock.getsockname()[1]
        before = sorted(str(path) for path in self.base.rglob('*'))
        result = subprocess.run([sys.executable, '-B', str(self.root / 'full_session.py'),
                                 '--config-json', json.dumps(self.config), '--check'],
                                capture_output=True, text=True, timeout=5,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout.split('R1_FULL_CHECK ')[-1])['ready'])
        self.assertEqual(before, sorted(str(path) for path in self.base.rglob('*')))

    def test_stop_confirmation_split_between_log_reads_is_preserved(self):
        process, run, prepared = self.prepare_control(
            "import time\nprint('Stop ',end='',flush=True)\ntime.sleep(.2)\nprint('confirmed.',flush=True)\n")
        process.wait(timeout=5)
        stdout, stderr = process.communicate()
        result = json.loads(stdout.split('R1_FULL_RESULT ')[-1])
        self.assertEqual(result['returncode'], 0)
        self.assertTrue(result['stop_confirmed'])

    def test_model_text_containing_stop_words_is_not_stop_confirmation(self):
        process, run, prepared = self.prepare_control(
            "print('model text: Stop confirmed.',flush=True)\nprint('Stop is NOT confirmed.',flush=True)\n")
        process.wait(timeout=5)
        stdout, stderr = process.communicate()
        result = json.loads(stdout.split('R1_FULL_RESULT ')[-1])
        self.assertEqual(result['returncode'], 0)
        self.assertFalse(result['stop_confirmed'])


if __name__ == '__main__':
    unittest.main()
