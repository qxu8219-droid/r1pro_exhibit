"""Local camera receiver and exhibition supervisor; standard library only."""
import argparse
from contextlib import ExitStack, contextmanager
from http.client import HTTPException
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import ProxyHandler, build_opener

from evidence import (FRESH_SECONDS, MAX_TELEMETRY_BYTES, _check_finite,
                      _reject_constant, _telemetry_state)
from launcher import LaunchError, NoRedirect, ensure_server, open_browser, stop_owned_server


class LiveError(ValueError):
    pass


def bridge_reachable(robot_ip, robot_port, timeout=2):
    """A listening bridge may still be warming up; never restart it for bad frames."""
    try:
        with socket.create_connection((robot_ip, robot_port), timeout=timeout):
            return True
    except OSError:
        return False


def start_remote_bridge(robot_ip, robot_user="nvidia", ssh_port=22,
                        bridge_script="/home/nvidia/r1pro_ws/vision_event_capture/live_stream/start_bridge.sh"):
    """Run the installed camera script over normal, interactive OpenSSH.

    No passwords are stored or passed in arguments. The remote guard prevents
    the existing script's restart logic from interrupting a bridge that came
    online after our local port check. Runtime logs/snapshots are the script's
    normal side effects; source and configuration files are never edited here.
    """
    try:
        address = str(ipaddress.ip_address(robot_ip))
    except ValueError as exc:
        raise LiveError("SSH 机器人 IP 无效。") from exc
    if not isinstance(robot_user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", robot_user):
        raise LiveError("SSH 用户名无效。")
    if type(ssh_port) is not int or not 1 <= ssh_port <= 65535:
        raise LiveError("SSH 端口必须在 1 到 65535 之间。")
    if (not isinstance(bridge_script, str) or not bridge_script.startswith("/")
            or any(ord(char) < 32 for char in bridge_script)):
        raise LiveError("机器人启动脚本必须是有效的绝对路径。")
    ssh = shutil.which("ssh")
    if ssh is None:
        raise LiveError("电脑未找到 ssh 客户端；请安装 OpenSSH，或用 --no-ssh 仅接收已有画面。")
    tcp_check = ("import socket,sys\ntry:\n"
                 " s=socket.create_connection(('127.0.0.1',8772),2);s.close()\n"
                 "except OSError:sys.exit(1)")
    command = ("if curl --noproxy '*' -fsS --max-time 2 http://127.0.0.1:8772/healthz "
               ">/dev/null 2>&1; then echo '相机桥已在运行，复用现有服务。'; "
               "elif python3 -B -c " + shlex.quote(tcp_check) + " >/dev/null 2>&1; "
               "then echo '相机端口已监听，等待现有服务，不执行重启。'; "
               "else bash " + shlex.quote(bridge_script) + "; fi")
    arguments = [ssh, "-T", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5",
                 "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=ask",
                 "-p", str(ssh_port), robot_user + "@" + address, command]
    print("通过 SSH 启动机器人已有相机桥；如提示请输入 SSH 密码（不会保存）。", flush=True)
    try:
        result = subprocess.run(arguments, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise LiveError("SSH 启动等待超时；请检查认证、网络和机器人脚本输出。") from exc
    except OSError as exc:
        raise LiveError("无法运行 SSH：%s" % exc) from exc
    if result.returncode:
        raise LiveError("机器人相机桥启动失败（SSH/脚本退出码 %s），请查看上方输出。" % result.returncode)


def fetch_snapshot(url, timeout=3):
    """Read and validate one source observation without changing its bytes or clock."""
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=timeout) as response:
            raw = response.read(MAX_TELEMETRY_BYTES + 1)
        if len(raw) > MAX_TELEMETRY_BYTES:
            raise LiveError("机器人快照超过 8 MiB，未写入本地文件。")
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        if data == {}:
            raise LiveError("机器人接口已连通，但相机尚未就绪（空快照）；请等待相机开始出帧。")
        _check_finite(data)
        state = _telemetry_state(data)
        if not all(state["images"].get(key) for key in ("head", "wrist_left", "wrist_right")):
            raise LiveError("等待头部、左腕、右腕三路有效相机画面。")
        if not 0 <= time.time() - state["observed_at"] <= FRESH_SECONDS:
            raise LiveError("机器人画面已过期或时间超前；请检查画面更新和两端时钟。")
        return raw
    except LiveError:
        raise
    except (OSError, HTTPException, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise LiveError("机器人快照读取或格式校验失败：%s" % exc) from exc


def atomic_write(path, raw):
    """Publish source bytes through a unique temporary file in the same directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def receiver_lock(path):
    """Hold one stable lock inode for the complete receiver lifetime."""
    lock = Path(str(path) + ".receiver.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LiveError("此快照已有实时接收器运行；请使用原启动窗口，或指定其他 --snapshot-file。") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description="接收机器人三路相机画面并启动本地实时展示")
    parser.add_argument("--robot-ip", default="172.18.21.239")
    parser.add_argument("--robot-port", type=int, default=8772)
    parser.add_argument("--robot-user", default="nvidia", help="机器人 SSH 用户名")
    parser.add_argument("--ssh-port", type=int, default=22, help="机器人 SSH 端口")
    parser.add_argument("--bridge-script", default="/home/nvidia/r1pro_ws/vision_event_capture/live_stream/start_bridge.sh",
                        help="机器人上已经安装的相机桥启动脚本绝对路径")
    parser.add_argument("--no-ssh", action="store_true", help="只接收已有画面，不尝试启动机器人相机桥")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--snapshot-file", default=str(Path.home() / "r1pro_live" / "snapshot.json"))
    parser.add_argument("--db")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check", action="store_true", help="只检查远端三路画面，不写文件或启动本地服务")
    parser.add_argument("--wait-seconds", type=float, default=40)
    args = parser.parse_args(argv)
    try:
        address = ipaddress.ip_address(args.robot_ip)
    except ValueError:
        parser.error("--robot-ip 必须为有效 IP 地址")
    if not all(1 <= value <= 65535 for value in (args.robot_port, args.port, args.ssh_port)):
        parser.error("端口必须在 1 到 65535 之间")
    if not math.isfinite(args.wait_seconds) or args.wait_seconds < 0:
        parser.error("--wait-seconds 必须为有限非负秒数")
    host = "[%s]" % address if address.version == 6 else str(address)
    source = "http://%s:%d/snapshot" % (host, args.robot_port)
    snapshot = Path(args.snapshot_file).expanduser().resolve()
    owned, previous_signals = None, {}

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    def stop_server():
        nonlocal owned
        stop_owned_server(owned)
        owned = None

    try:
        if args.check:
            fetch_snapshot(source)
            print("检查通过：头部、左腕、右腕画面有效且新鲜。", flush=True)
            return 0
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_signals[signum] = signal.signal(signum, interrupt)
        with ExitStack() as resources:
            resources.enter_context(receiver_lock(snapshot))
            # LIFO cleanup stops our child before another launcher can reuse
            # it through the just-released receiver lock.
            resources.callback(stop_server)
            print("等待机器人三路实时画面：" + source, flush=True)
            if not args.no_ssh and not bridge_reachable(str(address), args.robot_port):
                start_remote_bridge(str(address), robot_user=args.robot_user,
                                    ssh_port=args.ssh_port, bridge_script=args.bridge_script)
            deadline, last_error = time.monotonic() + args.wait_seconds, None
            while True:
                try:
                    raw = fetch_snapshot(source, timeout=min(3, max(.1, deadline - time.monotonic())))
                    atomic_write(snapshot, raw)
                    break
                except LiveError as exc:
                    if str(exc) != last_error:
                        print(str(exc), flush=True)
                        last_error = str(exc)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LiveError("等待有效三路画面超时：%s" % exc) from exc
                    time.sleep(min(1, remaining))
            owned = ensure_server(args.port, args.db, telemetry_file=snapshot)
            url = "http://127.0.0.1:%d/?source=live" % args.port
            print("\nR1 实时展示：" + url, flush=True)
            print("三路画面已接入；保持此窗口开启，按 Ctrl+C 停止本次接收器。", flush=True)
            if owned is None:
                print("已复用现有展示服务；退出接收器时保留该服务。", flush=True)
            if not args.no_browser and not open_browser(url, args.port):
                print("浏览器未能自动打开，请手动打开上方地址。", flush=True)
            last_error = None
            while True:
                if owned is not None and owned.poll() is not None:
                    raise LaunchError("本次启动的展示服务已退出（退出码 %s）。" % owned.returncode)
                started = time.monotonic()
                try:
                    atomic_write(snapshot, fetch_snapshot(source))
                    if last_error is not None:
                        print("实时画面接收已恢复。", flush=True)
                    last_error = None
                except (LiveError, OSError) as exc:
                    if str(exc) != last_error:
                        print("接收暂时中断，继续重试：%s" % exc, flush=True)
                        last_error = str(exc)
                time.sleep(max(0, 1 - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\n已停止本次实时接收器。", flush=True)
        return 0
    except (LiveError, LaunchError, OSError) as exc:
        print("启动失败：%s" % exc, file=sys.stderr, flush=True)
        return 1
    finally:
        stop_owned_server(owned)
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
