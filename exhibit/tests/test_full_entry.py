"""The full entry is exercised with local HTTP fixtures and a fake SSH client only."""
import base64
from contextlib import redirect_stdout, redirect_stderr
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import select
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'exhibit'))


class FullEntryTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'exhibit/full_entry.py').exists(), 'Full motion entry is missing')
        import full_entry
        self.entry = full_entry
        self.temp = tempfile.TemporaryDirectory(prefix='r1-full-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.live = self.root / 'live.json'
        self.full = self.root / 'full.json'
        self.live_config = dict(robot_ip='127.0.0.1', robot_user='nvidia', ssh_port=22,
            port=self.free_port(), snapshot_file=str(self.root/'data/snapshot.json'),
            db=str(self.root/'data/fleet.sqlite3'), robot_clock_offset='auto')
        self.full_config = dict(remote_script='/robot/full-demo/full_session.py',
            coordinator_dir='/robot/coordinator', robot_config='/robot/coordinator/config.yaml',
            vla_python='/robot/venv/bin/python', services_script='/robot/auto-services/start_services.py',
            tunnel_host='100.105.13.29', tunnel_user='ksadmin', tunnel_local_port=18128,
            tunnel_remote_port=18118)
        self.write_settings()
        self.env = dict(os.environ, R1_LIVE_SETTINGS=str(self.live), R1_FULL_SETTINGS=str(self.full),
                        PYTHONDONTWRITEBYTECODE='1')

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def write_settings(self):
        self.live.write_text(json.dumps(self.live_config))
        self.full.write_text(json.dumps(self.full_config))

    def config(self, argv=()):
        return self.entry.load_configuration(list(argv), self.live, self.full)

    def test_configuration_reuses_saved_live_target_and_paths(self):
        args, remote = self.config()
        self.assertEqual(args.robot_ip, self.live_config['robot_ip'])
        self.assertEqual(args.snapshot_file, self.live_config['snapshot_file'])
        self.assertEqual(args.db, self.live_config['db'])
        self.assertEqual(remote, self.full_config)

    def test_bad_configuration_fails_before_ssh(self):
        for field, value in [('password','secret'), ('robot_ip','127.0.0.2'),
                             ('tunnel_local_port',True), ('robot_config','relative.yaml'),
                             ('tunnel_host','-oProxyCommand=x')]:
            with self.subTest(field=field):
                self.full.write_text(json.dumps(dict(self.full_config, **{field:value})))
                with self.assertRaises(self.entry.FullEntryError):
                    self.config()
        self.write_settings()
        for argv in (['--no-ssh'], ['--no-vla'], ['--wait-seconds','nan'], ['--robot-ip','bad']):
            with self.subTest(argv=argv), self.assertRaises((self.entry.FullEntryError,SystemExit)):
                self.config(argv)

    def test_help_does_not_need_configuration(self):
        self.live.unlink(); self.full.unlink()
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.config(['--help'])
        self.assertEqual(caught.exception.code, 0)

    def test_source_gate_accepts_health_without_inference_and_rejects_stale_or_wrong_source(self):
        from vla_evidence import VLAStore
        path = self.root/'vla.sqlite3'
        store = VLAStore(path)
        try:
            store.ingest([], 'source-fixture', 0, health={'robot_id':'robot', 'active_session_id':None})
        finally:
            store.close()
        since = time.time()-.2
        self.assertTrue(self.entry.vla_source_ready(path, 'source-fixture', since))
        self.assertFalse(self.entry.vla_source_ready(path, 'wrong-source', since))
        self.assertFalse(self.entry.vla_source_ready(path, 'source-fixture', time.time()+1))
        self.assertFalse(self.entry.vla_source_ready(self.root/'absent.sqlite3', None, since))
        self.assertFalse((self.root/'absent.sqlite3').exists())

    def fake_ssh(self, mode='normal'):
        fake = self.root/'ssh'
        log = self.root/'ssh.jsonl'
        code = r'''import json,os,select,signal,sys,time
from pathlib import Path
args=sys.argv[1:]
log=Path(os.environ['FULL_TEST_LOG'])
with log.open('a') as out: out.write(json.dumps(args)+'\n')
path=Path(args[args.index('-S')+1])
if '-O' in args:
 op=args[args.index('-O')+1]
 if op=='check': sys.exit(0 if path.exists() else 1)
 if op=='exit':
  if path.exists():
   try:os.kill(int(path.read_text()),signal.SIGTERM)
   except ProcessLookupError:pass
  sys.exit(0)
if '-M' in args:
 path.write_text(str(os.getpid()))
 while True:time.sleep(.05)
command=args[-1]
if ('--run ' in command or '--release ' in command) and '--config-json' in command:
 print('run/release use only the saved session configuration',flush=True);sys.exit(28)
if ' -c ' in command:
 print('READY',flush=True)
 for line in sys.stdin:print(time.time(),flush=True)
 sys.exit(0)
if '--check' in command:
 print('R1_FULL_CHECK '+json.dumps({'ready':True}),flush=True);sys.exit(0)
if '--prepare' in command:
 if os.environ.get('FULL_TEST_MODE')=='prepare-failure':sys.exit(12)
 print('R1_FULL_PREPARED '+json.dumps({'token':'a'*32,'existing_controller':[123] if os.environ.get('FULL_TEST_MODE')=='existing' else [],'model':{'status':'ready','verified':os.environ.get('FULL_TEST_MODE')!='unverified-model'},'services_ready':True,'tunnel':{},'services':{'vla':{'source_id':'source-fixture','robot_id':'robot'}}}),flush=True);sys.exit(0)
if '--release' in command:
 print('R1_FULL_RELEASED {}',flush=True);sys.exit(0)
if '--run' in command:
 if os.environ.get('FULL_TEST_MODE')=='existing-race':
  print('R1_FULL_RESULT '+json.dumps({'token':'a'*32,'status':'existing_controller','existing_controller':[123],'owned':False,'stop_confirmed':False,'returncode':None}),flush=True);sys.exit(0)
 if select.select([sys.stdin],[],[],.1)[0]:sys.exit(16)
 print('R1_FULL_ARMED '+json.dumps({'token':'a'*32}),flush=True)
 if not select.select([sys.stdin],[],[],2)[0]:sys.exit(17)
 if sys.stdin.readline()!='R1_FULL_HEARTBEAT\n':sys.exit(18)
 Path(os.environ['FULL_TEST_RUN']).write_text('started')
 if os.environ.get('FULL_TEST_MODE')=='disconnect':sys.exit(255)
 if os.environ.get('FULL_TEST_MODE')=='wait-cancel':
  while True:
   chunk=os.read(0,1)
   if chunk==b'\x03':break
   if not chunk:sys.exit(255)
  time.sleep(4.3)
  if select.select([sys.stdin],[],[],0)[0]:sys.exit(19)
  print('R1_FULL_RESULT '+json.dumps({'token':'a'*32,'status':'cancelled','stop_confirmed':True,'returncode':0}),flush=True)
 else:print('R1_FULL_RESULT '+json.dumps({'token':'a'*32,'status':'completed','stop_confirmed':True,'returncode':0}),flush=True)
 sys.exit(0)
sys.exit(9)
'''
        fake.write_text('#!'+sys.executable+'\n'+code)
        fake.chmod(0o755)
        self.env.update(PATH=str(self.root)+os.pathsep+os.environ['PATH'], FULL_TEST_LOG=str(log),
                        FULL_TEST_RUN=str(self.root/'run.called'), FULL_TEST_MODE=mode)
        return log

    def sources(self, source='source-fixture'):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith('/events'):
                    result=dict(source_id=source,next_cursor=0,events=[],has_more=False,
                                health={'robot_id':'robot','active_session_id':None})
                else:
                    pixel='data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()
                    result=dict(schema_version=1,robot_id='robot',run_id='camera',observed_at=time.time()-.2,
                                task='fixture',model_id='fixture',stage='camera',
                                images={key:pixel for key in ('head','wrist_left','wrist_right')})
                raw=json.dumps(result).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def log_message(self,*_):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        self.live_config.update(robot_port=server.server_port,vla_port=server.server_port)
        self.write_settings()

    def invoke(self, argv=()):
        return subprocess.run([sys.executable,'-B',str(ROOT/'exhibit/full_entry.py'),
                               '--no-browser','--wait-seconds','3']+list(argv),
                              env=self.env,cwd=self.root,stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=15)

    def calls(self, log):
        return [json.loads(line) for line in log.read_text().splitlines()]

    def test_check_never_prepares_or_starts_local_or_motion_services(self):
        log=self.fake_ssh()
        result=self.invoke(['--check'])
        self.assertEqual(result.returncode,0,result.stdout)
        commands=[call[-1] for call in self.calls(log)]
        self.assertTrue(any('--check' in cmd for cmd in commands),commands)
        self.assertFalse(any('--prepare' in cmd or '--run ' in cmd for cmd in commands),commands)
        self.assertFalse((self.root/'data').exists())
        self.assertFalse((self.root/'run.called').exists())

    def test_complete_fake_ssh_and_real_local_display_order_and_owned_cleanup(self):
        self.sources();log=self.fake_ssh()
        result=self.invoke()
        self.assertEqual(result.returncode,0,result.stdout)
        calls=self.calls(log);commands=[call[-1] for call in calls]
        prepare=next(i for i,cmd in enumerate(commands) if '--prepare' in cmd)
        run=next(i for i,cmd in enumerate(commands) if '--run ' in cmd)
        release=next(i for i,cmd in enumerate(commands) if '--release ' in cmd)
        self.assertLess(prepare,run);self.assertLess(run,release)
        self.assertTrue((self.root/'run.called').exists())
        self.assertTrue((self.root/'data/vla.sqlite3').exists())
        self.assertTrue(any('-O' in call and 'exit' in call for call in calls))
        with socket.socket() as sock:self.assertNotEqual(sock.connect_ex(('127.0.0.1',self.live_config['port'])),0)

    def test_prepare_failure_and_wrong_vla_source_never_run_motion(self):
        self.sources(source='wrong-source');log=self.fake_ssh('prepare-failure')
        result=self.invoke();self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertFalse((self.root/'run.called').exists())
        self.env['FULL_TEST_MODE']='normal'
        result=self.invoke();self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertFalse((self.root/'run.called').exists())
        self.assertTrue(any('--release ' in call[-1] for call in self.calls(log)))

    def test_occupied_display_port_is_preserved_and_motion_never_runs(self):
        self.sources();log=self.fake_ssh()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',self.live_config['port']));listener.listen()
            result=self.invoke()
            self.assertNotEqual(result.returncode,0,result.stdout)
            self.assertFalse((self.root/'run.called').exists())
            self.assertGreaterEqual(listener.fileno(),0)
        self.assertTrue(any('--release ' in call[-1] for call in self.calls(log)))

    def test_disconnected_run_reports_unknown_not_stopped(self):
        self.sources();self.fake_ssh('disconnect')
        result=self.invoke()
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertIn('未知',result.stdout)

    def test_unverified_model_report_cannot_start_motion(self):
        self.sources();self.fake_ssh('unverified-model')
        result=self.invoke()
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertFalse((self.root/'run.called').exists())

    def test_password_prompt_without_newline_is_forwarded_before_input(self):
        # Waiting for readline() here would hide an inner SSH password prompt
        # forever. The fake remote refuses to finish until input arrives.
        remote = "import sys;sys.stdout.write('password: ');sys.stdout.flush();sys.stdin.readline();print('\\nPROMPT_DONE {}',flush=True)"
        code = 'import sys;sys.path.insert(0,%r);import full_entry;full_entry.stream_remote(%r,"PROMPT_DONE")' % (
            str(ROOT/'exhibit'), [sys.executable,'-u','-c',remote])
        proc=subprocess.Popen([sys.executable,'-B','-c',code],stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        try:
            received=b'';deadline=time.monotonic()+3
            while b'password: ' not in received and time.monotonic()<deadline:
                if select.select([proc.stdout],[],[],.1)[0]:received+=os.read(proc.stdout.fileno(),4096)
            self.assertIn(b'password: ',received)
            proc.stdin.write(b'fixture-password\n');proc.stdin.flush()
            output=proc.communicate(timeout=3)[0]
            self.assertEqual(proc.returncode,0,output)
            self.assertNotIn(b'fixture-password',received+output)
        finally:
            if proc.poll() is None:proc.kill();proc.wait(timeout=2)
            for pipe in (proc.stdin,proc.stdout):pipe.close()

    def test_receiver_lock_conflict_preserves_owner_and_never_runs_motion(self):
        self.sources();self.fake_ssh()
        path=Path(self.live_config['snapshot_file']+'.receiver.lock')
        path.parent.mkdir()
        with path.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=self.invoke()
            self.assertNotEqual(result.returncode,0,result.stdout)
            self.assertFalse((self.root/'run.called').exists())
            with path.open('a') as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)

    def test_prepare_cancellation_wins_over_a_success_marker_and_keeps_release_token(self):
        ready=self.root/'prepare-output'
        script="import json,sys,time;from pathlib import Path;print('R1_FULL_PREPARED '+json.dumps({'token':'a'*32}),flush=True);Path(%r).touch();sys.stdin.buffer.read(1);time.sleep(.1)" % str(ready)
        def cancel():
            deadline=time.monotonic()+3
            while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
            time.sleep(.05)
            os.kill(os.getpid(),signal.SIGTERM)
        worker=threading.Thread(target=cancel);worker.start()
        try:
            with redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt) as caught:
                self.entry.stream_remote([sys.executable,'-u','-c',script],'R1_FULL_PREPARED')
            self.assertEqual(caught.exception.result['token'],'a'*32)
        finally:
            worker.join(timeout=3)

    def test_local_fresh_telemetry_without_three_images_never_passes_gate(self):
        args,_=self.config();args.wait_seconds=0
        with patch.object(self.entry,'read_local_json',return_value={'connected':True,'state':{'robot_id':'robot','images':{}}}), \
             patch.object(self.entry,'vla_source_ready',return_value=True):
            with self.assertRaises(self.entry.FullEntryError):
                self.entry.wait_display(args,None,'source-fixture','robot',time.time())

    def test_motion_confirmation_must_be_the_final_remote_output(self):
        result=dict(token='a'*32,status='completed',stop_confirmed=True,returncode=0)
        script="import sys;print(%r,flush=True);print('controller still running',flush=True);sys.exit(255)" % ('R1_FULL_RESULT '+json.dumps(result))
        with redirect_stdout(io.StringIO()), self.assertRaises(self.entry.FullEntryError):
            self.entry.stream_remote([sys.executable,'-u','-c',script],
                                     'R1_FULL_RESULT',motion=True,token='a'*32)

    def test_matching_existing_display_and_controller_are_observed_without_taking_ownership(self):
        self.sources();self.fake_ssh('existing')
        args,_=self.config()
        display=self.entry.start_display(args,0)
        try:
            self.entry.wait_display(args,display,'source-fixture','robot',time.time())
            for mode in ('existing','existing-race'):
                self.env['FULL_TEST_MODE']=mode
                result=self.invoke()
                self.assertEqual(result.returncode,0,result.stdout)
                self.assertIn('复用',result.stdout)
                self.assertIn('仅观察',result.stdout)
                self.assertFalse((self.root/'run.called').exists())
                self.assertIsNone(display.poll())
                self.assertTrue(self.entry.read_local_json(args.port,'/api/telemetry')['connected'])
        finally:
            self.entry.stop_owned_server(display)

    def test_signal_waits_past_four_seconds_for_remote_normal_stop(self):
        self.sources();self.fake_ssh('wait-cancel')
        with (self.root/'output.log').open('w+') as output:
            proc=subprocess.Popen([sys.executable,'-B',str(ROOT/'exhibit/full_entry.py'),'--no-browser','--wait-seconds','3'],
                                  env=self.env,cwd=self.root,stdin=subprocess.DEVNULL,stdout=output,stderr=output)
            try:
                deadline=time.monotonic()+7
                while not (self.root/'run.called').exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.03)
                self.assertTrue((self.root/'run.called').exists())
                started=time.monotonic();proc.send_signal(signal.SIGTERM);proc.wait(timeout=10)
                self.assertGreaterEqual(time.monotonic()-started,4.2)
                output.seek(0);self.assertEqual(proc.returncode,0,output.read())
            finally:
                if proc.poll() is None:proc.kill();proc.wait(timeout=3)


if __name__=='__main__':unittest.main()
