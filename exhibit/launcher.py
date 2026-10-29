"""Start the local exhibition server and a kiosk browser; Python 3.9+, stdlib only."""
import argparse
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
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
import webbrowser
from instance_identity import instance_identity
from robot_clock import robot_clock_offset as validate_robot_clock_offset


ROOT = Path(__file__).resolve().parent
FLEET_SIZE = 4


class LaunchError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _server_path(value):
    """Match the server child's working directory when resolving relative paths."""
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def expected_identity(db=None, telemetry_file=None, vla_db=None, robot_clock_offset=0.0):
    db_path = (ROOT / "data" / "fleet-four.sqlite3" if db is None else
               ":memory:" if str(db) == ":memory:" else _server_path(db))
    telemetry_path = None if telemetry_file is None else _server_path(telemetry_file)
    vla_path = None if vla_db is None else _server_path(vla_db)
    return instance_identity(ROOT, db_path, FLEET_SIZE, telemetry_path, vla_db=vla_path,
                             robot_clock_offset=robot_clock_offset)


def probe(port, db=None, telemetry_file=None, vla_db=None, robot_clock_offset=0.0):
    """Return absent/exhibit/incompatible/occupied without modifying any service."""
    robot_clock_offset = validate_robot_clock_offset(robot_clock_offset)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            pass
    except OSError:
        return "absent"
    # Local preview must not go through a configured corporate HTTP proxy.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        url = "http://127.0.0.1:%d" % port
        with opener.open(url + "/api/health", timeout=1) as response:
            health = json.loads(response.read(4096))
        with opener.open(url + "/", timeout=1) as response:
            page = response.read(128 * 1024)
        if health.get("ok") is True and health.get("simulation") is True:
            if page == (ROOT / "static" / "index.html").read_bytes():
                matches = (health.get("fleet_size") == FLEET_SIZE and
                           health.get("instance_identity") == expected_identity(
                               db, telemetry_file, vla_db, robot_clock_offset=robot_clock_offset))
                return "exhibit" if matches else "incompatible"
    except (OSError, ValueError, AttributeError):
        pass
    return "occupied"


def stop_owned_server(process):
    """Only terminate the child process we created, never a process found by port."""
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        process.terminate()
    else:
        process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=4)


def ensure_server(port, db=None, timeout=10, telemetry_file=None, vla_db=None, robot_clock_offset=0.0):
    robot_clock_offset = validate_robot_clock_offset(robot_clock_offset)
    state = probe(port, db, telemetry_file, vla_db, robot_clock_offset=robot_clock_offset)
    if state == "exhibit":
        return None
    if state == "incompatible":
        raise LaunchError("端口 %d 已被旧版或其他配置的展示服务占用；本启动器需要同一目录、数据库和实时配置的四机服务。"
                          "请手动关闭旧服务后重试，或使用 --port 8772。" % port)
    if state == "occupied":
        raise LaunchError("端口 %d 已被其他程序占用；请关闭占用程序，或使用 --port 8772。" % port)
    command = [sys.executable, str(ROOT / "server.py"), "--port", str(port),
               "--fleet-size", str(FLEET_SIZE)]
    if db is not None:
        command.extend(["--db", str(db)])
    if telemetry_file is not None:
        command.extend(["--telemetry-file", str(telemetry_file)])
    if vla_db is not None:
        command.extend(["--vla-db", str(vla_db)])
    if robot_clock_offset:
        command.extend(["--robot-clock-offset", str(robot_clock_offset)])
    options = {"cwd": str(ROOT), "env": dict(os.environ, PYTHONUTF8="1")}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise LaunchError("服务未能启动（退出码 %s）；请查看上面的错误信息。" % process.returncode)
            if probe(port, db, telemetry_file, vla_db, robot_clock_offset=robot_clock_offset) == "exhibit" and process.poll() is None:
                return process
            time.sleep(0.15)
        raise LaunchError("等待本地服务启动超时；请检查端口和数据目录权限。")
    except BaseException:
        stop_owned_server(process)
        raise


def find_browser():
    for name in ("msedge", "chrome", "google-chrome", "google-chrome-stable",
                 "chromium", "chromium-browser", "microsoft-edge", "microsoft-edge-stable"):
        executable = shutil.which(name)
        if executable:
            return executable
    candidates = []
    if sys.platform == "win32":
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.extend(Path(base) / suffix for suffix in (
                    "Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"))
    elif sys.platform == "darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            candidates.extend(base / suffix for suffix in (
                "Google Chrome.app/Contents/MacOS/Google Chrome",
                "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"))
    return next((str(path) for path in candidates if path.is_file()), None)


def browser_command(executable, url, port):
    # A dedicated profile makes kiosk flags work even when a personal browser is open.
    profile = Path(tempfile.gettempdir()) / ("r1-exhibit-browser-%d" % port)
    command = [executable, "--user-data-dir=" + str(profile), "--no-first-run",
               "--no-default-browser-check", "--disable-session-crashed-bubble",
               "--new-window", "--kiosk", url]
    if "edge" in Path(executable).name.lower():
        command.append("--edge-kiosk-type=fullscreen")
    return command


def open_browser(url, port):
    executable = find_browser()
    if executable:
        try:
            subprocess.Popen(browser_command(executable, url, port),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    print("未找到可启动的 Chrome / Edge；改用默认浏览器，打开后按 F11 全屏。", flush=True)
    try:
        return bool(webbrowser.open(url))
    except webbrowser.Error:
        return False


def main(argv=None):
    if sys.version_info < (3, 9):
        print("需要 Python 3.9 或更新版本。", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description="启动 R1 展示台并打开全屏浏览器")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--db", help="可选：使用指定的本地模拟数据库；相对路径以 exhibit 目录为准")
    parser.add_argument("--telemetry-file", help="可选：读取本机实时快照；相对路径以 exhibit 目录为准")
    parser.add_argument("--vla-db", help="可选：读取独立 VLA 事件数据库")
    parser.add_argument("--robot-clock-offset", type=validate_robot_clock_offset, default=0.0,
                        help="机器人时钟减去本机时钟的秒数（-300 到 300）；只校正实时数据年龄")
    parser.add_argument("--no-browser", action="store_true", help="只启动服务，不打开浏览器")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    owned = None

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    previous_signals = {}
    for signum in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if signum is not None:
            previous_signals[signum] = signal.signal(signum, interrupt)
    try:
        owned = ensure_server(args.port, args.db, telemetry_file=args.telemetry_file, vla_db=args.vla_db,
                              robot_clock_offset=args.robot_clock_offset)
        url = "http://127.0.0.1:%d/" % args.port
        print("\nR1 展示台：" + url, flush=True)
        if owned is None:
            print("已复用现有展示服务；本窗口退出不会停止它。", flush=True)
        else:
            print("保持此窗口开启；按 Ctrl+C 停止本次启动的服务。", flush=True)
        if not args.no_browser and not open_browser(url, args.port):
            print("浏览器未能自动打开，请手动打开上方地址。", flush=True)
        print("关闭展示浏览器窗口：Alt+F4（Mac：⌘W）。", flush=True)
        # A reused service belongs to its original launcher; no second owner is needed.
        if owned is None:
            return 0
        while owned.poll() is None:
            time.sleep(0.5)
        raise LaunchError("展示服务已退出（退出码 %s）；请查看上面的错误信息。" % owned.returncode)
    except KeyboardInterrupt:
        print("\n已停止本次启动器。", flush=True)
        return 0
    except (LaunchError, OSError) as exc:
        print("启动失败：%s" % exc, file=sys.stderr, flush=True)
        return 1
    finally:
        stop_owned_server(owned)
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
