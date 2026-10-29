"""Opt-in remote service startup, tested only with fake SSH and loopback."""
import base64
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_launcher as live


SCRIPT = "/home/nvidia/r1_exhibit_observer/auto-services/start_services.py"


class RemoteServicesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="R1 auto services ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.ssh_log = self.root / "ssh.jsonl"
        self.snapshot = self.root / "snapshot.json"
        self.output = io.StringIO()
        self.env = dict(os.environ, PATH=str(self.bin), SERVICES_SSH_LOG=str(self.ssh_log),
                        SERVICES_SNAPSHOT=str(self.snapshot), SERVICES_SSH_STATUS="0")
        self.write_executable("ssh", """import fcntl, json, os, pathlib, sys
snapshot = pathlib.Path(os.environ['SERVICES_SNAPSHOT'])
lock = pathlib.Path(str(snapshot) + '.receiver.lock')
locked = False
if lock.exists():
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            locked = True
with open(os.environ['SERVICES_SSH_LOG'], 'a') as stream:
    stream.write(json.dumps(dict(argv=sys.argv[1:], locked=locked,
                                snapshot_exists=snapshot.exists())) + '\\n')
sys.exit(int(os.environ['SERVICES_SSH_STATUS']))
""")

    def write_executable(self, name, source):
        executable = self.bin / name
        executable.write_text("#!" + sys.executable + "\n" + source)
        executable.chmod(0o755)

    def helper(self):
        start = getattr(live, "start_remote_services", None)
        self.assertTrue(callable(start), "opt-in remote services startup is missing")
        return start

    def calls(self):
        return [json.loads(line) for line in self.ssh_log.read_text().splitlines()] if self.ssh_log.exists() else []

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def listener(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        self.addCleanup(sock.close)
        return sock.getsockname()[1]

    def frame(self, age=0):
        image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
        return json.dumps(dict(schema_version=1, robot_id="loopback", run_id="services-test",
            task="camera test", model_id="no model", stage="camera", observed_at=time.time() - age,
            images={key: image for key in ("head", "wrist_left", "wrist_right")})).encode()

    def camera(self, body=None, status=200):
        test = self

        class Camera(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(test.frame() if body is None else body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Camera)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)

        self.addCleanup(close)
        return server.server_port

    def args(self, camera_port, vla_port, *extra):
        return ["--robot-ip", "127.0.0.1", "--robot-port", str(camera_port),
                "--vla-port", str(vla_port), "--port", str(self.free_port()),
                "--snapshot-file", str(self.snapshot), "--db", str(self.root / "fleet.sqlite3"),
                "--services-script", SCRIPT, "--wait-seconds", "0", *extra]

    def run_main(self, args):
        self.assertTrue(hasattr(live.build_parser().parse_args([]), "services_script"),
                        "launcher does not accept the remote services option")
        with patch.dict(os.environ, self.env, clear=True), redirect_stdout(self.output), redirect_stderr(self.output):
            return live.main(args)

    def test_services_startup_is_opt_in(self):
        args = live.build_parser().parse_args([])
        self.assertIsNone(getattr(args, "services_script", "missing"))
        command = self.args(self.free_port(), self.free_port(), "--no-vla")
        del command[command.index("--services-script"):command.index("--services-script") + 2]
        self.assertEqual(self.run_main(command), 1)
        self.assertEqual(self.calls(), [])

    def test_ssh_preserves_quoted_script_and_connection_options(self):
        start = self.helper()
        marker = self.root / "injected"
        script = "/robot/services '$(touch " + str(marker) + ")/start.py"
        with patch.dict(os.environ, self.env, clear=True), redirect_stdout(self.output):
            start("2001:db8::73", robot_user="qa-user", ssh_port=2207, script=script,
                  camera_port=8872, vla_port=8874)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[-2], "qa-user@2001:db8::73")
        self.assertEqual(argv[argv.index("-p") + 1], "2207")
        self.assertIn("StrictHostKeyChecking=ask", argv)
        self.assertIn("ConnectTimeout=5", argv)
        self.assertNotIn("StrictHostKeyChecking=no", argv)
        self.assertFalse(any("password" in value.lower() or "sshpass" in value.lower() for value in argv))
        python_log = self.root / "python.json"
        self.write_executable("python3", "import json,sys\nopen(" + repr(str(python_log)) + ", 'w').write(json.dumps(sys.argv[1:]))\n")
        result = subprocess.run(["/bin/sh", "-c", argv[-1]], env=self.env, capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(python_log.read_text()),
                         ["-B", script, "--camera-port", "8872", "--vla-port", "8874"])
        self.assertFalse(marker.exists())

    def test_camera_only_passes_no_vla_request_to_remote_script(self):
        start = self.helper()
        with patch.dict(os.environ, self.env, clear=True), redirect_stdout(self.output):
            start("127.0.0.1", script=SCRIPT, camera_only=True)
        self.assertEqual(shlex.split(self.calls()[0]["argv"][-1]),
                         ["python3", "-B", SCRIPT, "--camera-port", "8772", "--vla-port", "8774", "--camera-only"])

    def test_invalid_remote_arguments_never_launch_ssh(self):
        start = self.helper()
        cases = [{"robot_ip": "localhost; touch bad"}, {"robot_user": "-oProxyCommand=bad"},
                 {"robot_user": "name\nother"}, {"ssh_port": 0}, {"ssh_port": True},
                 {"camera_port": 65536}, {"camera_port": "8772"}, {"vla_port": -1},
                 {"script": "relative/start.py"}, {"script": ""}, {"script": None},
                 {"script": "/tmp/bad\nscript.py"}, {"script": "/tmp/bad\x00script.py"}]
        with patch.dict(os.environ, self.env, clear=True):
            for invalid in cases:
                values = dict(robot_ip="127.0.0.1", script=SCRIPT)
                values.update(invalid)
                with self.subTest(invalid=invalid), self.assertRaises(live.LiveError):
                    start(**values)
        self.assertEqual(self.calls(), [])

    def test_ssh_failure_and_missing_client_report_actionable_errors(self):
        start = self.helper()
        with patch.dict(os.environ, dict(self.env, SERVICES_SSH_STATUS="23"), clear=True), redirect_stdout(self.output):
            with self.assertRaisesRegex(live.LiveError, "23"):
                start("127.0.0.1", script=SCRIPT)
        self.assertEqual(len(self.calls()), 1)
        (self.bin / "ssh").unlink()
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(live.LiveError, "SSH|ssh"):
                start("127.0.0.1", script=SCRIPT)
        self.assertEqual(len(self.calls()), 1)

    def test_ssh_start_timeout_is_bounded_and_not_retried(self):
        start = self.helper()
        seen = []

        def timeout(command, timeout):
            seen.append(timeout)
            raise subprocess.TimeoutExpired(command, timeout)

        with patch.dict(os.environ, self.env, clear=True), patch.object(live.subprocess, "run", side_effect=timeout), redirect_stdout(self.output):
            with self.assertRaisesRegex(live.LiveError, "超时"):
                start("127.0.0.1", script=SCRIPT)
        self.assertEqual(len(seen), 1)
        self.assertGreaterEqual(seen[0], 20)
        self.assertLessEqual(seen[0], 60)

    def test_absent_camera_starts_once_inside_lock_before_read_and_never_fakes_ready(self):
        args = self.args(self.free_port(), self.free_port(), "--no-vla")
        args[args.index("--wait-seconds") + 1] = "1.1"
        self.assertEqual(self.run_main(args), 1)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["locked"])
        self.assertFalse(calls[0]["snapshot_exists"])
        self.assertIn("--camera-only", shlex.split(calls[0]["argv"][-1]))
        self.assertFalse(self.snapshot.exists())
        self.assertFalse((self.root / "fleet.sqlite3").exists())
        self.assertIn("超时", self.output.getvalue())

    def test_absent_vla_starts_once_without_old_observer_fallback(self):
        camera_port = self.camera()
        with patch.object(live, "open_browser", side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_main(self.args(camera_port, self.free_port())), 0)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(shlex.split(calls[0]["argv"][-1])[:3], ["python3", "-B", SCRIPT])
        self.assertNotIn("--camera-only", shlex.split(calls[0]["argv"][-1]))
        self.assertEqual(json.loads(self.snapshot.read_bytes())["run_id"], "services-test")

    def test_listening_services_with_bad_or_stale_camera_never_restart(self):
        vla_port = self.listener()
        for body, status in ((b"{}", 200), (b"broken", 200), (self.frame(age=60), 200), (b"{}", 503)):
            with self.subTest(body=body[:40], status=status):
                self.assertEqual(self.run_main(self.args(self.camera(body, status), vla_port)), 1)
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.snapshot.exists())

    def test_camera_only_reuses_camera_without_starting_absent_vla(self):
        with patch.object(live, "open_browser", side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_main(self.args(self.camera(), self.free_port(), "--no-vla")), 0)
        self.assertEqual(self.calls(), [])
        self.assertTrue(self.snapshot.exists())
        self.assertFalse((self.root / "vla.sqlite3").exists())

    def test_check_and_no_ssh_never_start_remote_services(self):
        for flag in ("--check", "--no-ssh"):
            with self.subTest(flag=flag):
                self.assertEqual(self.run_main(self.args(self.free_port(), self.free_port(), flag)), 1)
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.snapshot.exists())
        self.assertFalse((self.root / "fleet.sqlite3").exists())

    def test_existing_receiver_lock_prevents_remote_start(self):
        with live.receiver_lock(self.snapshot):
            self.assertEqual(self.run_main(self.args(self.free_port(), self.free_port())), 1)
        self.assertEqual(self.calls(), [])

    def test_failed_remote_start_stops_before_snapshot_and_is_not_retried(self):
        self.env["SERVICES_SSH_STATUS"] = "29"
        self.assertEqual(self.run_main(self.args(self.camera(), self.free_port())), 1)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse(self.snapshot.exists())
        self.assertIn("29", self.output.getvalue())

    def test_invalid_script_or_user_is_rejected_even_when_ports_listen(self):
        camera_port, vla_port = self.camera(), self.listener()
        for option, value in (("--services-script", "relative.py"), ("--robot-user", "-bad")):
            with self.subTest(option=option):
                args = self.args(camera_port, vla_port) + [option + "=" + value]
                with self.assertRaises(SystemExit) as failure:
                    self.run_main(args)
                self.assertEqual(failure.exception.code, 2)
                self.assertFalse(self.snapshot.exists())
                self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
