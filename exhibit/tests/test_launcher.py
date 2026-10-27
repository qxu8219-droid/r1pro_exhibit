import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
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
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher


@contextlib.contextmanager
def local_http(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class OtherApp(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true, "simulation": true}' if self.path == "/api/health" else b"other app")

    def log_message(self, *_args):
        pass


class LauncherTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX shell launcher")
    def test_shell_launcher_from_path_with_spaces_and_termination_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="R1 shell test ") as directory:
            folder = Path(directory)
            linked = folder / "exhibition with spaces"
            linked.symlink_to(launcher.ROOT, target_is_directory=True)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            command = [str(linked / "run.sh"), "--no-browser", "--port", str(port),
                       "--db", str(folder / "shell.sqlite3")]
            process = subprocess.Popen(command, cwd=directory, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while launcher.probe(port, folder / "shell.sqlite3") != "exhibit" and time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail("shell launcher exited before server was ready")
                    time.sleep(0.1)
                self.assertEqual(launcher.probe(port, folder / "shell.sqlite3"), "exhibit")
            finally:
                process.terminate()
                process.wait(timeout=10)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(launcher.probe(port), "absent")

    def test_owned_server_starts_with_space_in_database_path_and_reuses_without_ownership(self):
        with tempfile.TemporaryDirectory(prefix="R1 launcher test ") as directory:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            db = Path(directory) / "database with spaces.sqlite3"
            owned = launcher.ensure_server(port, db)
            try:
                self.assertIsNotNone(owned)
                self.assertTrue(db.is_file())
                self.assertEqual(launcher.probe(port, db), "exhibit")
                opener = launcher.build_opener(launcher.ProxyHandler({}))
                with opener.open("http://127.0.0.1:%d/api/health" % port, timeout=2) as response:
                    self.assertEqual(json.load(response)["fleet_size"], 4)
                with opener.open("http://127.0.0.1:%d/api/state" % port, timeout=2) as response:
                    self.assertEqual({robot["id"] for robot in json.load(response)["robots"]},
                                     {"R1", "R2", "R3", "R4"})
                reused = launcher.ensure_server(port, db)
                self.assertIsNone(reused)
                launcher.stop_owned_server(reused)
                self.assertIsNone(owned.poll())
                self.assertEqual(launcher.probe(port, db), "exhibit")
            finally:
                launcher.stop_owned_server(owned)
            self.assertIsNotNone(owned.poll())
            self.assertEqual(launcher.probe(port), "absent")

    def test_occupied_port_is_rejected_without_starting_or_stopping_any_process(self):
        with local_http(OtherApp) as port, patch.object(launcher.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(launcher.LaunchError, "已被其他程序占用"):
                launcher.ensure_server(port)
            popen.assert_not_called()
            self.assertEqual(launcher.probe(port), "occupied")

    def test_same_page_with_two_robot_or_missing_identity_is_rejected_without_stopping_it(self):
        for fleet_size in (2, None, 4):
            with self.subTest(fleet_size=fleet_size):
                health = {"ok": True, "simulation": True}
                if fleet_size is not None:
                    health["fleet_size"] = fleet_size

                class WrongFleet(OtherApp):
                    def do_GET(self):
                        self.send_response(200)
                        self.end_headers()
                        payload = (json.dumps(health).encode("utf-8") if self.path == "/api/health"
                                   else (launcher.ROOT / "static" / "index.html").read_bytes())
                        self.wfile.write(payload)

                with local_http(WrongFleet) as port, \
                        patch.object(launcher.subprocess, "Popen") as popen, \
                        patch.object(launcher, "stop_owned_server") as stop:
                    self.assertEqual(launcher.probe(port), "incompatible")
                    with self.assertRaisesRegex(launcher.LaunchError, "旧版或其他配置.*四机服务"):
                        launcher.ensure_server(port)
                    popen.assert_not_called()
                    stop.assert_not_called()
                    self.assertEqual(launcher.probe(port), "incompatible")

    def test_matching_page_from_another_directory_or_database_is_not_reused(self):
        with tempfile.TemporaryDirectory(prefix="R1 中文交付 audit ") as directory:
            folder = Path(directory)
            db = folder / "当前数据.sqlite3"
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            owned = launcher.ensure_server(port, db)
            try:
                with patch.object(launcher.subprocess, "Popen") as popen, \
                        patch.object(launcher, "stop_owned_server") as stop:
                    other_db = folder / "另一份数据.sqlite3"
                    with self.assertRaisesRegex(launcher.LaunchError, "其他配置"):
                        launcher.ensure_server(port, other_db)
                    self.assertFalse(other_db.exists())
                    other_root = folder / "另一份包 空格"
                    (other_root / "static").mkdir(parents=True)
                    (other_root / "static" / "index.html").write_bytes(
                        (launcher.ROOT / "static" / "index.html").read_bytes())
                    with patch.object(launcher, "ROOT", other_root):
                        with self.assertRaisesRegex(launcher.LaunchError, "其他配置"):
                            launcher.ensure_server(port, db)
                    popen.assert_not_called()
                    stop.assert_not_called()
                self.assertIsNone(owned.poll())
                self.assertEqual(launcher.probe(port, db), "exhibit")
            finally:
                launcher.stop_owned_server(owned)

    def test_telemetry_configuration_must_match_before_reuse(self):
        with tempfile.TemporaryDirectory(prefix="R1 live config ") as directory:
            folder = Path(directory)
            db = folder / "state.sqlite3"
            telemetry = folder / "只读 快照.json"
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            owned = launcher.ensure_server(port, db, telemetry_file=telemetry)
            try:
                self.assertEqual(launcher.probe(port, db, telemetry), "exhibit")
                self.assertIsNone(launcher.ensure_server(port, db, telemetry_file=telemetry))
                for requested in (None, folder / "other.json"):
                    with self.subTest(telemetry=requested), self.assertRaisesRegex(launcher.LaunchError, "实时配置"):
                        launcher.ensure_server(port, db, telemetry_file=requested)
                self.assertFalse(telemetry.exists())
                self.assertIsNone(owned.poll())
            finally:
                launcher.stop_owned_server(owned)

    def test_relative_configuration_paths_match_the_child_working_directory(self):
        relative_db = Path("data") / "中文 数据.sqlite3"
        relative_telemetry = Path("data") / "只读.json"
        self.assertEqual(launcher.expected_identity(relative_db, relative_telemetry),
                         launcher.expected_identity(launcher.ROOT / relative_db,
                                                    launcher.ROOT / relative_telemetry))

    def test_probe_does_not_follow_http_redirects(self):
        paths = []

        class Redirect(OtherApp):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/must-not-follow")
                self.end_headers()

        with local_http(Redirect) as port:
            self.assertEqual(launcher.probe(port), "occupied")
        self.assertEqual(paths, ["/api/health"])

    def test_failed_start_reports_error_and_never_opens_browser(self):
        child = Mock(returncode=2)
        child.poll.return_value = 2
        with patch.object(launcher, "probe", return_value="absent"), \
                patch.object(launcher.subprocess, "Popen", return_value=child), \
                patch.object(launcher, "open_browser") as browser, \
                contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(launcher.main(["--no-browser"]), 1)
        browser.assert_not_called()
        child.terminate.assert_not_called()
        self.assertIn("退出码 2", output.getvalue())

    def test_start_timeout_cleans_up_owned_child(self):
        child = Mock()
        child.poll.return_value = None
        with patch.object(launcher, "probe", return_value="absent"), \
                patch.object(launcher.subprocess, "Popen", return_value=child), \
                patch.object(launcher, "stop_owned_server") as stop:
            with self.assertRaisesRegex(launcher.LaunchError, "启动超时"):
                launcher.ensure_server(12345, timeout=0)
            stop.assert_called_once_with(child)

    def test_edge_command_keeps_paths_intact_and_requests_kiosk_window(self):
        browser = "C:/Program Files/Microsoft/Edge/Application/msedge.exe"
        command = launcher.browser_command(browser, "http://127.0.0.1:8771/", 8771)
        self.assertEqual(command[0], browser)
        self.assertIn("--kiosk", command)
        self.assertIn("--edge-kiosk-type=fullscreen", command)
        self.assertIn("http://127.0.0.1:8771/", command)
        self.assertEqual(sum(arg.startswith("--user-data-dir=") for arg in command), 1)

    def test_default_browser_fallback_reports_manual_fullscreen(self):
        with patch.object(launcher, "find_browser", return_value=None), \
                patch.object(launcher.webbrowser, "open", return_value=True) as browser, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(launcher.open_browser("http://127.0.0.1:8771/", 8771))
        browser.assert_called_once_with("http://127.0.0.1:8771/")
        self.assertIn("F11", output.getvalue())


if __name__ == "__main__":
    unittest.main()
