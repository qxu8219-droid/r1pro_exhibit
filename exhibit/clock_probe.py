"""Read the robot's wall clock through one short-lived, read-only SSH command."""
import ipaddress
import math
import os
import re
import select
import shlex
import subprocess
import time

from robot_clock import robot_clock_offset


AUTH_TIMEOUT_S = 30.0
SAMPLE_TIMEOUT_S = 3.0
MAX_RTT_S = 1.0
CLOCK_JUMP_TOLERANCE_S = 0.05

# No files, imports from the robot project, subprocesses, or robot APIs. -B
# prevents Python bytecode writes; stdin carries only the three literal pings.
REMOTE_CODE = """import sys, time
print('READY', flush=True)
for _ in range(3):
    if sys.stdin.readline() != 'ping\\n':
        raise SystemExit(2)
    print(repr(time.time()), flush=True)
"""


class ClockProbeError(RuntimeError):
    """A trustworthy clock offset could not be measured."""


def _validate_target(ip, user, ssh_port):
    if not isinstance(ip, str) or "%" in ip:
        raise ClockProbeError("机器人 IP 必须是有效的 IPv4 或 IPv6 地址。")
    try:
        address = str(ipaddress.ip_address(ip))
    except ValueError as exc:
        raise ClockProbeError("机器人 IP 必须是有效的 IPv4 或 IPv6 地址。") from exc
    if not isinstance(user, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", user) is None:
        raise ClockProbeError("SSH 用户名格式无效：须以字母或下划线开头，仅含字母、数字、下划线、点或短横线。")
    if type(ssh_port) is not int or not 1 <= ssh_port <= 65535:
        raise ClockProbeError("SSH 端口必须是 1 到 65535 之间的整数。")
    return address


def _read_line(stream, deadline, phase):
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise ClockProbeError("SSH 时钟探测%s超时，请检查连接或手动指定 --robot-clock-offset。" % phase)
        chunk = os.read(stream.fileno(), 256)
        if not chunk:
            raise ClockProbeError("SSH 时钟探测连接提前退出，请先检查上方 SSH 网络/连接错误，再检查认证和机器人上的 python3。")
        data.extend(chunk)
        if len(data) > 128:
            raise ClockProbeError("SSH 时钟探测收到过长的异常输出。")
        if b"\n" in data:
            line, remainder = data.split(b"\n", 1)
            if remainder:
                raise ClockProbeError("SSH 时钟探测收到非预期的额外输出。")
            try:
                return line.decode("ascii")
            except UnicodeDecodeError as exc:
                raise ClockProbeError("SSH 时钟探测输出格式无效。") from exc


def _send_ping(stream, deadline):
    pending = b"ping\n"
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [stream], [], remaining)[1]:
            raise ClockProbeError("SSH 时钟探测发送请求超时。")
        try:
            written = os.write(stream.fileno(), pending)
        except BlockingIOError:
            continue
        if written == 0:
            raise ClockProbeError("SSH 时钟探测连接已关闭。")
        pending = pending[written:]


def _stop_process(process):
    """Always reap the owned SSH child, including errors and KeyboardInterrupt."""
    for stream in (process.stdin, process.stdout):
        try:
            stream.close()
        except OSError:
            pass
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        except ProcessLookupError:
            process.wait(timeout=1)


def measure_robot_clock(ip, user="nvidia", ssh_port=22, *, ssh_control_path=None):
    """Return a conservative robot-minus-local offset and the minimum RTT.

    The remote timestamp lies between local send and receive times. Returning
    remote_time - local_send_time makes corrected observation ages slightly
    older (by at most the RTT), never artificially fresher. Authentication is
    excluded by waiting for READY before taking any sample. This reads only
    time.time(); it never uses an observation timestamp as the current time.

    SSH uses the user's normal identity configuration and terminal prompts.
    There is no password storage, connection retry, or robot-service startup.
    """
    address = _validate_target(ip, user, ssh_port)
    if (ssh_control_path is not None and (not isinstance(ssh_control_path, str)
            or not ssh_control_path.startswith('/')
            or any(ord(char) < 32 for char in ssh_control_path))):
        raise ClockProbeError('SSH 控制连接路径必须是有效的绝对路径。')
    command = ["ssh", "-T", "-S", ssh_control_path or "none",
               "-o", "StrictHostKeyChecking=ask", "-o", "BatchMode=no",
               "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
               "-o", "PermitLocalCommand=no", "-o", "ClearAllForwardings=yes",
               "-o", "RemoteCommand=none", "-p", str(ssh_port), "-l", user,
               "--", address, "python3 -u -B -c " + shlex.quote(REMOTE_CODE)]
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=None, bufsize=0,
                                   env=dict(os.environ, SSH_ASKPASS_REQUIRE="never"))
        os.set_blocking(process.stdin.fileno(), False)
        ready = _read_line(process.stdout, time.monotonic() + AUTH_TIMEOUT_S, "认证/就绪")
        if ready != "READY":
            raise ClockProbeError("SSH 时钟探测未收到 READY，远端输出不符合预期。")
        baseline_mono = time.monotonic()
        baseline_wall = time.time()
        samples = []
        lower_bound, upper_bound = -math.inf, math.inf
        for _ in range(3):
            start_mono = time.monotonic()
            start_wall = time.time()
            deadline = start_mono + SAMPLE_TIMEOUT_S
            _send_ping(process.stdin, deadline)
            line = _read_line(process.stdout, deadline, "采样")
            end_wall = time.time()
            end_mono = time.monotonic()
            for wall, mono in ((start_wall, start_mono), (end_wall, end_mono)):
                if abs((wall - baseline_wall) - (mono - baseline_mono)) > CLOCK_JUMP_TOLERANCE_S:
                    raise ClockProbeError("本地时钟在探测期间发生跳变，请稳定系统时钟后重试。")
            rtt = end_mono - start_mono
            try:
                robot_time = float(line)
            except ValueError as exc:
                raise ClockProbeError("SSH 时钟探测输出不是有效时间戳。") from exc
            if not math.isfinite(robot_time):
                raise ClockProbeError("SSH 时钟探测输出不是有限时间戳。")
            upper = robot_time - start_wall
            lower = robot_time - end_wall
            lower_bound = max(lower_bound, lower)
            upper_bound = min(upper_bound, upper)
            if lower_bound > upper_bound + CLOCK_JUMP_TOLERANCE_S:
                raise ClockProbeError("机器人时钟在探测期间发生跳变，请稳定系统时钟后重试。")
            samples.append({"offset_s": upper, "rtt_s": rtt})
        process.stdin.close()
        try:
            code = process.wait(timeout=1)
        except subprocess.TimeoutExpired as exc:
            raise ClockProbeError("SSH 时钟探测完成后未正常退出，已停止探测。") from exc
        if code != 0:
            raise ClockProbeError("SSH 时钟探测异常退出（状态 %d），请检查终端错误信息。" % code)
        result = min(samples, key=lambda sample: sample["rtt_s"])
        if result["rtt_s"] > MAX_RTT_S:
            raise ClockProbeError("SSH 时钟探测三次采样的最短 RTT 仍超过 1 秒，无法可靠校时，请检查网络后重试。")
        try:
            result["offset_s"] = robot_clock_offset(result["offset_s"])
        except ValueError as exc:
            raise ClockProbeError("测得机器人时差超出允许的 ±300 秒，请先校准系统时钟。") from exc
        return result
    except OSError as exc:
        raise ClockProbeError("SSH 时钟探测失败，请确认已安装 OpenSSH 并检查连接：%s" % exc) from exc
    finally:
        if process is not None:
            _stop_process(process)
