"""Clock probing through a local SSH substitute; no robot/network access."""
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    clock_probe = importlib.import_module("clock_probe")
except ModuleNotFoundError:
    clock_probe = None


FAKE_SSH = r'''
import json, os, pathlib, shlex, signal, sys, time
root = pathlib.Path(os.environ["CLOCK_FAKE_ROOT"])
(root / "pid").write_text(str(os.getpid()))
with (root / "calls").open("a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\n")
mode = os.environ.get("CLOCK_FAKE_MODE", "normal")
if mode == "exit":
    sys.exit(255)
if mode == "no_ready":
    time.sleep(60)
time.sleep(float(os.environ.get("CLOCK_FAKE_HANDSHAKE", "0")))
if mode == "bad_ready":
    print("not the clock helper", flush=True)
    time.sleep(60)
if mode in ("no_sample", "bad_sample", "long_sample"):
    print("READY", flush=True)
    sys.stdin.readline()
    if mode == "bad_sample":
        print(os.environ.get("CLOCK_FAKE_SAMPLE", "not a timestamp"), flush=True)
    if mode == "long_sample":
        print("9" * 9000, flush=True)
    time.sleep(60)
command = shlex.split(sys.argv[-1])
if command[:4] != ["python3", "-u", "-B", "-c"] or len(command) != 5:
    print("unexpected remote command", flush=True)
    sys.exit(2)
real_time = time.time
offset = float(os.environ.get("CLOCK_FAKE_OFFSET", "13.5"))
delays = [float(value) for value in os.environ.get("CLOCK_FAKE_DELAYS", "0,0,0").split(",")]
count = 0
def robot_time():
    global count
    time.sleep(delays[count])
    value = real_time() + offset
    if mode == "clock_jump" and count > 0:
        value += 5
    count += 1
    with (root / "samples").open("a") as stream:
        stream.write(str(count) + "\n")
    return value
time.time = robot_time
exec(compile(command[4], "<remote-clock>", "exec"))
if count != 3:
    sys.exit(3)
if mode == "hang_exit":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
'''


class ClockProbeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(clock_probe, "clock_probe helper is not implemented")
        self.temp = tempfile.TemporaryDirectory(prefix="r1-clock-probe-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ssh = self.root / "ssh"
        ssh.write_text("#!" + sys.executable + "\n" + FAKE_SSH)
        ssh.chmod(0o755)
        self.environment = patch.dict(os.environ, {
            "PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
            "CLOCK_FAKE_ROOT": str(self.root),
            "CLOCK_FAKE_MODE": "normal", "CLOCK_FAKE_OFFSET": "13.5",
            "CLOCK_FAKE_HANDSHAKE": "0", "CLOCK_FAKE_DELAYS": "0,0,0",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def assert_child_gone(self):
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_ready_excludes_authentication_delay_and_minimum_rtt_is_conservative(self):
        # Including authentication in a sample, choosing the last RTT, or using
        # midpoint subtraction would violate these independently known bounds.
        with patch.dict(os.environ, {"CLOCK_FAKE_HANDSHAKE": ".35",
                                    "CLOCK_FAKE_DELAYS": ".18,.02,.12"}):
            started = time.monotonic()
            result = clock_probe.measure_robot_clock("192.0.2.1", "robot_user", 2222)
        self.assertGreater(time.monotonic() - started, .6)
        self.assertGreaterEqual(result["offset_s"], 13.5)
        self.assertLess(result["offset_s"], 13.6)
        self.assertGreaterEqual(result["rtt_s"], .02)
        self.assertLess(result["rtt_s"], .1)
        calls = [json.loads(line) for line in (self.root / "calls").read_text().splitlines()]
        self.assertEqual(len(calls), 1, "probing must not reconnect or retry SSH")
        command = calls[0]
        self.assertIn("-T", command)
        self.assertIn("StrictHostKeyChecking=ask", command)
        self.assertIn("2222", command)
        self.assertIn("robot_user", command)
        self.assert_child_gone()

    def test_negative_robot_offset_is_preserved(self):
        with patch.dict(os.environ, {"CLOCK_FAKE_OFFSET": "-13.5"}):
            result = clock_probe.measure_robot_clock("::1")
        self.assertGreaterEqual(result["offset_s"], -13.5)
        self.assertLess(result["offset_s"], -13.4)
        self.assert_child_gone()

    def test_owned_ssh_control_socket_is_used_without_new_authentication(self):
        try:
            result = clock_probe.measure_robot_clock('192.0.2.1', ssh_control_path='/tmp/owned socket')
        except TypeError as exc:
            self.fail('Clock probe must support the full launcher owned SSH connection: '+str(exc))
        self.assertGreaterEqual(result['offset_s'], 13.5)
        args = json.loads((self.root / 'calls').read_text().splitlines()[0])
        self.assertEqual(args[args.index('-S')+1], '/tmp/owned socket')
        self.assert_child_gone()

    def test_invalid_ssh_parameters_are_rejected_before_process_creation(self):
        invalid = [
            ("-oProxyCommand=bad", "nvidia", 22), ("robot.local", "nvidia", 22),
            ("127.0.0.1;touch bad", "nvidia", 22), ("127.0.0.1\n", "nvidia", 22),
            ("fe80::1%eth0", "nvidia", 22), (None, "nvidia", 22),
            ("127.0.0.1", "-oUser=bad", 22), ("127.0.0.1", "x;id", 22),
            ("127.0.0.1", "", 22), ("127.0.0.1", "x\ny", 22),
            ("127.0.0.1", "x", 0), ("127.0.0.1", "x", 65536),
            ("127.0.0.1", "x", True), ("127.0.0.1", "x", 22.1),
            ("127.0.0.1", "x", "22"),
        ]
        for ip, user, port in invalid:
            with self.subTest(ip=ip, user=user, port=port):
                with self.assertRaises(clock_probe.ClockProbeError):
                    clock_probe.measure_robot_clock(ip, user, port)
        self.assertFalse((self.root / "calls").exists())

    def test_authentication_timeout_cleans_up_and_does_not_restart(self):
        with patch.dict(os.environ, {"CLOCK_FAKE_MODE": "no_ready"}), \
                patch.object(clock_probe, "AUTH_TIMEOUT_S", .2):
            started = time.monotonic()
            with self.assertRaisesRegex(clock_probe.ClockProbeError, "超时"):
                clock_probe.measure_robot_clock("192.0.2.1")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len((self.root / "calls").read_text().splitlines()), 1)
        self.assert_child_gone()

    def test_sample_timeout_cleans_up(self):
        with patch.dict(os.environ, {"CLOCK_FAKE_MODE": "no_sample"}), \
                patch.object(clock_probe, "SAMPLE_TIMEOUT_S", .2):
            with self.assertRaisesRegex(clock_probe.ClockProbeError, "超时"):
                clock_probe.measure_robot_clock("192.0.2.1")
        self.assert_child_gone()

    def test_malformed_or_nonfinite_output_is_rejected_and_cleaned_up(self):
        cases = [("bad_ready", ""), ("bad_sample", "garbage"),
                 ("bad_sample", "nan"), ("bad_sample", "inf"),
                 ("bad_sample", "-inf"), ("long_sample", "")]
        for mode, value in cases:
            with self.subTest(mode=mode, value=value), patch.dict(os.environ, {
                    "CLOCK_FAKE_MODE": mode, "CLOCK_FAKE_SAMPLE": value}):
                with self.assertRaises(clock_probe.ClockProbeError):
                    clock_probe.measure_robot_clock("192.0.2.1")
                self.assert_child_gone()

    def test_ssh_failure_is_reported_without_retry(self):
        with patch.dict(os.environ, {"CLOCK_FAKE_MODE": "exit"}):
            with self.assertRaisesRegex(clock_probe.ClockProbeError, "SSH"):
                clock_probe.measure_robot_clock("192.0.2.1")
        self.assertEqual(len((self.root / "calls").read_text().splitlines()), 1)
        self.assert_child_gone()

    def test_remote_clock_jump_is_rejected(self):
        with patch.dict(os.environ, {"CLOCK_FAKE_MODE": "clock_jump"}):
            with self.assertRaisesRegex(clock_probe.ClockProbeError, "跳变"):
                clock_probe.measure_robot_clock("192.0.2.1")
        self.assert_child_gone()

    def test_local_clock_jump_is_rejected(self):
        real_time = time.time
        count = 0

        def jumped_time():
            nonlocal count
            count += 1
            return real_time() + (5 if count > 1 else 0)

        with patch.object(clock_probe.time, "time", jumped_time):
            with self.assertRaisesRegex(clock_probe.ClockProbeError, "跳变"):
                clock_probe.measure_robot_clock("192.0.2.1")
        self.assert_child_gone()

    def test_offset_outside_supported_range_is_rejected(self):
        for offset in ("301", "-301"):
            with self.subTest(offset=offset), patch.dict(os.environ, {"CLOCK_FAKE_OFFSET": offset}):
                with self.assertRaisesRegex(clock_probe.ClockProbeError, "300"):
                    clock_probe.measure_robot_clock("192.0.2.1")
                self.assert_child_gone()

    def test_slow_first_sample_uses_fastest_of_three_completed_samples(self):
        with patch.dict(os.environ, {"CLOCK_FAKE_DELAYS": "1.1,.02,.12"}):
            result = clock_probe.measure_robot_clock("192.0.2.1")
        self.assertGreaterEqual(result["offset_s"], 13.5)
        self.assertLess(result["offset_s"], 13.6)
        self.assertGreaterEqual(result["rtt_s"], .02)
        self.assertLess(result["rtt_s"], .1)
        self.assertEqual((self.root / "samples").read_text().splitlines(), ["1", "2", "3"])
        self.assertEqual(len((self.root / "calls").read_text().splitlines()), 1)
        self.assert_child_gone()

    def test_slow_later_sample_does_not_discard_fast_sample(self):
        with patch.dict(os.environ, {"CLOCK_FAKE_DELAYS": ".02,1.1,.12"}):
            result = clock_probe.measure_robot_clock("192.0.2.1")
        self.assertGreaterEqual(result["offset_s"], 13.5)
        self.assertLess(result["offset_s"], 13.6)
        self.assertGreaterEqual(result["rtt_s"], .02)
        self.assertLess(result["rtt_s"], .1)
        self.assertEqual((self.root / "samples").read_text().splitlines(), ["1", "2", "3"])
        self.assert_child_gone()

    def test_all_three_rtts_over_one_second_are_rejected(self):
        with patch.dict(os.environ, {"CLOCK_FAKE_DELAYS": "1.1,1.1,1.1"}):
            with self.assertRaisesRegex(clock_probe.ClockProbeError, "RTT"):
                clock_probe.measure_robot_clock("192.0.2.1")
        self.assertEqual((self.root / "samples").read_text().splitlines(), ["1", "2", "3"])
        self.assertEqual(len((self.root / "calls").read_text().splitlines()), 1)
        self.assert_child_gone()

    def test_remote_clock_jump_in_slow_sample_is_still_rejected(self):
        with patch.dict(os.environ, {"CLOCK_FAKE_MODE": "clock_jump",
                                    "CLOCK_FAKE_DELAYS": ".02,1.1,.02"}):
            with self.assertRaisesRegex(clock_probe.ClockProbeError, "跳变"):
                clock_probe.measure_robot_clock("192.0.2.1")
        self.assert_child_gone()

    def test_unresponsive_process_is_killed_after_measurement(self):
        with patch.dict(os.environ, {"CLOCK_FAKE_MODE": "hang_exit"}):
            started = time.monotonic()
            with self.assertRaisesRegex(clock_probe.ClockProbeError, "退出"):
                clock_probe.measure_robot_clock("192.0.2.1")
        self.assertLess(time.monotonic() - started, 4)
        self.assert_child_gone()

    def test_ctrl_c_cleans_up_running_ssh(self):
        script = ("import sys; sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) +
                  "); from clock_probe import measure_robot_clock; measure_robot_clock('192.0.2.1')")
        with patch.dict(os.environ, {"CLOCK_FAKE_MODE": "no_sample"}):
            driver = subprocess.Popen([sys.executable, "-B", "-c", script],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      start_new_session=True)
        try:
            deadline = time.monotonic() + 3
            while not (self.root / "pid").exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.root / "pid").exists())
            driver.send_signal(signal.SIGINT)
            driver.communicate(timeout=4)
            self.assertNotEqual(driver.returncode, 0)
            self.assert_child_gone()
        finally:
            if driver.poll() is None:
                driver.kill()
                driver.communicate(timeout=2)


if __name__ == "__main__":
    unittest.main()
