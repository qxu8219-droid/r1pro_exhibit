"""Explicit full-demo entry: prepare observation before running the original controller once.

Only the remote helper owns robot cancellation. The local display's bounded
shutdown must never be applied to the SSH session supervising robot motion.
"""
import codecs
from contextlib import closing, contextmanager
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import termios
import time
import tty
from urllib.request import ProxyHandler, build_opener

from clock_probe import ClockProbeError, measure_robot_clock
from evidence import MAX_TELEMETRY_BYTES
from launcher import NoRedirect, expected_identity, open_browser, probe, stop_owned_server
from live_entry import EntryError, SETTINGS as LIVE_SETTINGS, load_settings
import live_launcher

ROOT = Path(__file__).resolve().parent
FULL_SETTINGS = ROOT.parent / 'full_demo_settings.json'
DEFAULT_REMOTE_SCRIPT = '/home/nvidia/r1_exhibit_observer/full-demo/full_session.py'
FULL_FIELDS = {'remote_script', 'coordinator_dir', 'robot_config', 'vla_python',
               'services_script', 'tunnel_host', 'tunnel_user',
               'tunnel_local_port', 'tunnel_remote_port'}
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class FullEntryError(ValueError):
    pass


class RemoteCancelled(KeyboardInterrupt):
    def __init__(self, result):
        super().__init__('remote command cancelled')
        self.result = result


def load_configuration(argv, live_path=LIVE_SETTINGS, full_path=FULL_SETTINGS):
    """Validate all saved parameters before opening even a read-only connection."""
    parser = live_launcher.build_parser()
    parser.description = '完整演示：准备隧道、模型和实时展示后，执行一次原 start.sh --enable-motion'
    parser.epilog = '读取 live_settings.json 与 full_demo_settings.json；--check 严格只读，不启动服务或运动。'
    if '--help' in argv or '-h' in argv:
        parser.parse_args(argv)
    live = load_settings(live_path)
    # A full run always measures time afresh; never reuse a saved historical offset.
    live.pop('robot_clock_offset', None)
    base = [item for key, value in live.items()
            for item in ('--' + key.replace('_', '-'), str(value))]
    args = parser.parse_args(base + list(argv))
    try:
        args.robot_ip = str(ipaddress.ip_address(args.robot_ip))
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', args.robot_user):
            raise ValueError('机器人 SSH 用户名无效')
        if not all(1 <= port <= 65535 for port in (args.port, args.robot_port, args.vla_port, args.ssh_port)):
            raise ValueError('端口必须在 1 到 65535 之间')
        if not math.isfinite(args.wait_seconds) or args.wait_seconds < 0:
            raise ValueError('等待时间必须是有限非负数')
        if args.no_ssh or args.no_vla:
            raise ValueError('完整入口必须使用 SSH 并接入 VLA；只看画面请使用 start_live.sh')
        args.snapshot_file = str(Path(args.snapshot_file).expanduser().resolve())
        args.vla_db = str(Path(args.vla_db).expanduser().resolve()) if args.vla_db else str(Path(args.snapshot_file).with_name('vla.sqlite3'))
        path = Path(full_path).expanduser().resolve()
        raw = path.read_bytes()
        if len(raw) > 16384:
            raise ValueError('完整演示配置文件过大')
        full = json.loads(raw)
        if not isinstance(full, dict) or set(full) - FULL_FIELDS:
            raise ValueError('完整演示配置含未知字段；机器人连接只在 live_settings.json 配置，不能保存密码')
        full.setdefault('remote_script', DEFAULT_REMOTE_SCRIPT)
        if set(full) != FULL_FIELDS:
            raise ValueError('完整演示配置缺少字段：' + ', '.join(sorted(FULL_FIELDS - set(full))))
        for key, value in full.items():
            if key.endswith('_port'):
                if type(value) is not int or not 1 <= value <= 65535:
                    raise ValueError(key + ' 必须是有效整数端口')
            elif not isinstance(value, str) or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError(key + ' 必须是有效非空文本')
        for key in ('remote_script', 'coordinator_dir', 'robot_config', 'vla_python', 'services_script'):
            if not full[key].startswith('/'):
                raise ValueError(key + ' 必须是绝对路径')
        ipaddress.ip_address(full['tunnel_host'])
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', full['tunnel_user']):
            raise ValueError('隧道 SSH 用户名无效')
        return args, full
    except (OSError, ValueError, TypeError) as exc:
        raise FullEntryError('完整演示配置无效：' + str(exc)) from exc


class SSHConnection:
    """One privately owned ControlMaster, with no password persistence."""
    def __init__(self, args):
        self.args = args
        self.directory = None
        self.control_path = None
        self.master = None

    def command(self, *options):
        return ['ssh', '-S', self.control_path,
                '-o', 'StrictHostKeyChecking=ask', '-o', 'ConnectTimeout=10',
                '-o', 'ConnectionAttempts=1', '-o', 'PermitLocalCommand=no',
                '-o', 'ClearAllForwardings=yes', '-o', 'RemoteCommand=none',
                '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3',
                '-p', str(self.args.ssh_port), '-l', self.args.robot_user,
                *options, '--', self.args.robot_ip]

    def __enter__(self):
        if shutil.which('ssh') is None:
            raise FullEntryError('电脑未安装 OpenSSH')
        self.directory = tempfile.TemporaryDirectory(prefix='r1-full-ssh-')
        self.control_path = str(Path(self.directory.name) / 'control')
        try:
            print('连接机器人；SSH 密码和首次主机确认在终端输入，不保存密码。', flush=True)
            self.master = subprocess.Popen(self.command('-M', '-N', '-o', 'ControlPersist=no', '-o', 'BatchMode=no'),
                                           stdout=subprocess.DEVNULL,
                                           env=dict(os.environ, SSH_ASKPASS_REQUIRE='never'))
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if self.master.poll() is not None:
                    raise FullEntryError('机器人 SSH 认证或连接失败')
                if Path(self.control_path).exists():
                    checked = subprocess.run(self.command('-O', 'check'), stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, timeout=2)
                    if checked.returncode == 0:
                        return self
                time.sleep(.1)
            raise FullEntryError('等待机器人 SSH 认证超时')
        except BaseException:
            self.close()
            raise

    def remote_command(self, full, action, token=None):
        command = ['python3', '-u', '-B', full['remote_script']]
        if action in ('--check', '--prepare'):
            config = {key: value for key, value in full.items() if key != 'remote_script'}
            command.extend(['--config-json', json.dumps(config, ensure_ascii=True, separators=(',', ':'))])
        command.append(action)
        if token is not None:
            command.append(token)
        return self.command('-tt', '-o', 'BatchMode=yes', '-o', 'ControlMaster=no') + [shlex.join(command)]

    def close(self):
        if self.master is not None:
            if self.master.poll() is None:
                try:
                    subprocess.run(self.command('-O', 'exit'), stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                try:
                    self.master.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    # This is only our idle local master, never a motion child.
                    self.master.terminate()
                    try:
                        self.master.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.master.kill()
                        self.master.wait()
            self.master = None
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None

    def __exit__(self, *_):
        self.close()


@contextmanager
def terminal_input():
    """Forward terminal bytes without echoing nested SSH passwords locally."""
    fd, previous = None, None
    try:
        try:
            fd = sys.stdin.fileno()
            if os.isatty(fd):
                previous = termios.tcgetattr(fd)
                tty.setraw(fd)
        except (AttributeError, OSError, ValueError):
            fd = None
        yield fd
    finally:
        if previous is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def stream_remote(command, marker, motion=False, cancel_requested=None, token=None):
    """Chunk-stream an interactive SSH command, preserving its normal Ctrl+C path.

    stdin is piped to a remote forced terminal, so local signals can request
    remote cancellation with the terminal's interrupt byte. After a request we
    keep receiving until the helper reports its result or SSH disconnects.
    There is deliberately no cancellation timeout or kill for a motion session.
    """
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=None, bufsize=0,
                               start_new_session=True,
                               env=dict(os.environ, SSH_ASKPASS_REQUIRE='never'))
    previous, cancelled = {}, False
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    line_buffer, result = '', None
    armed, next_heartbeat = False, 0.0

    def cancel(_signum=None, _frame=None):
        nonlocal cancelled
        if cancelled:
            return
        cancelled = True
        print('\n请求远端按原流程取消；等待其结果确认。', flush=True)
        try:
            os.write(process.stdin.fileno(), b'\x03')
        except (OSError, ValueError):
            pass

    def consume(text):
        nonlocal line_buffer, result, armed
        sys.stdout.write(text)
        sys.stdout.flush()
        line_buffer += text
        while '\n' in line_buffer:
            line, line_buffer = line_buffer.split('\n', 1)
            line = line.rstrip('\r')
            if line.strip():
                # The protocol result must be the remote helper's final line,
                # not a similar string appearing earlier in controller logs.
                result = None
            if motion and line.startswith('R1_FULL_ARMED '):
                try:
                    announcement = json.loads(line[len('R1_FULL_ARMED '):])
                    if isinstance(announcement, dict) and announcement.get('token') == token:
                        armed = True
                except ValueError:
                    pass
            if line.startswith(marker + ' '):
                try:
                    value = json.loads(line[len(marker) + 1:])
                    if isinstance(value, dict):
                        result = value
                except ValueError:
                    pass
        # Logs may contain long progress lines; retain only a bounded tail.
        line_buffer = line_buffer[-65536:]

    try:
        for signum in SIGNALS:
            previous[signum] = signal.signal(signum, cancel)
        with terminal_input() as input_fd:
            while True:
                if cancel_requested is not None and cancel_requested():
                    cancel()
                if motion and armed and not cancelled and time.monotonic() >= next_heartbeat:
                    try:
                        os.write(process.stdin.fileno(), b'R1_FULL_HEARTBEAT\n')
                        next_heartbeat = time.monotonic() + 2
                    except OSError:
                        cancel()
                readers = [process.stdout]
                if input_fd is not None:
                    readers.append(input_fd)
                ready = select.select(readers, [], [], .2)[0]
                if process.stdout in ready:
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk:
                        break
                    consume(decoder.decode(chunk))
                if input_fd is not None and input_fd in ready:
                    data = os.read(input_fd, 4096)
                    if not data:
                        input_fd = None
                    else:
                        if b'\x03' in data:
                            cancel()
                            data = data.replace(b'\x03', b'')
                        if data:
                            try:
                                os.write(process.stdin.fileno(), data)
                            except OSError:
                                input_fd = None
            consume(decoder.decode(b'', final=True))
            if line_buffer.strip():
                consume('\n')
            code = process.wait()
    finally:
        # Disconnecting our channel invokes the helper's HUP handling. Never
        # turn an unconfirmed motion session into a local forced-stop claim.
        for pipe in (process.stdin, process.stdout):
            pipe.close()
        if process.poll() is None:
            process.wait()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if motion:
        if result is not None and result.get('token') != token:
            result = None
        observed_existing = (result is not None and result.get('status') == 'existing_controller'
                             and result.get('owned') is False
                             and isinstance(result.get('existing_controller'), list)
                             and bool(result['existing_controller']))
        if observed_existing:
            result['_local_cancelled'] = cancelled
        elif result is None or result.get('stop_confirmed') is not True or result.get('status') not in ('completed', 'cancelled', 'failed'):
            raise FullEntryError('机器人状态未知或未确认停止：未获得远端正常结束确认；请检查原控制终端，不能认为机器人已停止')
    elif cancelled:
        # Even if preparation finished at the same instant, user cancellation
        # must win over success. Keep its token available for owned cleanup.
        raise RemoteCancelled(result)
    elif code != 0 or result is None:
        raise FullEntryError('远端步骤失败或缺少结果确认（退出码 %s，%s）' % (code, marker))
    return result


def read_local_json(port, endpoint):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open('http://127.0.0.1:%d%s' % (port, endpoint), timeout=1) as response:
        raw = response.read(MAX_TELEMETRY_BYTES + 4097)
    if len(raw) > MAX_TELEMETRY_BYTES + 4096:
        raise ValueError('本地接口响应过大')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('本地接口响应格式无效')
    return value


def vla_source_ready(path, source_id, since, robot_id=None):
    """Read committed receiver health, never requiring an inference event."""
    try:
        path = Path(path)
        if not path.is_file():
            return False
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
            rows = db.execute('SELECT source_id,health,received_at FROM vla_sources WHERE received_at>=?', (since,)).fetchall()
        now = time.time()
        for source, raw, received in rows:
            health = json.loads(raw)
            if (isinstance(health, dict) and (source_id is None or source == source_id)
                    and 0 <= now - received <= 3
                    and (robot_id is None or health.get('robot_id') == robot_id)):
                return True
    except (OSError, ValueError, sqlite3.Error):
        pass
    return False


def display_state(args, measured_offset):
    state = probe(args.port, args.db, args.snapshot_file, args.vla_db,
                  robot_clock_offset=measured_offset)
    if state == 'incompatible':
        # Another launch may have measured a slightly different offset. Verify
        # all actual path identities using its exact offset before reusing it.
        try:
            telemetry = read_local_json(args.port, '/api/telemetry')
            health = read_local_json(args.port, '/api/health')
            # The existing server deliberately omits this diagnostic at zero.
            previous = telemetry.get('robot_clock_offset_s', 0.0)
            if (type(previous) in (int, float) and math.isfinite(previous)
                    and abs(previous - measured_offset) <= .5
                    and telemetry.get('connected') is True
                    and health.get('instance_identity') == expected_identity(
                        args.db, args.snapshot_file, args.vla_db, robot_clock_offset=previous)):
                return 'exhibit'
        except (OSError, ValueError):
            pass
    return state


def start_display(args, offset):
    state = display_state(args, offset)
    if state == 'exhibit':
        print('复用健康且配置匹配的本机展示；退出时保留该展示和接收器。', flush=True)
        return None
    if state != 'absent':
        raise FullEntryError('本机展示端口被其他进程或不兼容配置占用；未启动运动，请使用原窗口或更换端口')
    command = [sys.executable, '-B', str(ROOT / 'live_launcher.py'), '--no-ssh', '--no-browser',
               '--robot-ip', args.robot_ip, '--robot-user', args.robot_user,
               '--ssh-port', str(args.ssh_port), '--robot-port', str(args.robot_port),
               '--vla-port', str(args.vla_port), '--port', str(args.port),
               '--snapshot-file', args.snapshot_file, '--vla-db', args.vla_db,
               '--robot-clock-offset', str(offset), '--wait-seconds', str(args.wait_seconds)]
    if args.db is not None:
        command.extend(['--db', args.db])
    return subprocess.Popen(command, cwd=str(ROOT), start_new_session=True,
                            env=dict(os.environ, PYTHONUTF8='1'))


def wait_display(args, display, source_id, robot_id, since):
    deadline = time.monotonic() + args.wait_seconds
    while True:
        if display is not None and display.poll() is not None:
            raise FullEntryError('本次展示接收器启动失败；未执行运动（退出码 %s）' % display.returncode)
        try:
            telemetry = read_local_json(args.port, '/api/telemetry')
            state = telemetry.get('state') or {}
            images = state.get('images') or {}
            if (telemetry.get('connected') is True
                    and all(isinstance(images.get(key), str) and images[key].startswith('data:image/')
                            and ';base64,' in images[key] and images[key].split(';base64,', 1)[1]
                            for key in ('head', 'wrist_left', 'wrist_right'))
                    and (robot_id is None or state.get('robot_id') == robot_id)
                    and vla_source_ready(args.vla_db, source_id, since, robot_id)):
                return
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise FullEntryError('等待本机三路画面及指定 VLA 采集源接入超时；未执行运动')
        time.sleep(.1)


def observe_existing(display):
    print('检测到已有控制进程；仅观察，不重复启动、不接管或停止。', flush=True)
    if display is None:
        return 0
    while display.poll() is None:
        time.sleep(.5)
    raise FullEntryError('本次展示接收器已退出；已有控制进程保持原状态')


def main(argv=None):
    previous, display, prepared, connection = {}, None, None, None
    unknown_motion = False

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    try:
        for signum in SIGNALS:
            previous[signum] = signal.signal(signum, interrupt)
        args, full = load_configuration(sys.argv[1:] if argv is None else argv,
            os.environ.get('R1_LIVE_SETTINGS', LIVE_SETTINGS), os.environ.get('R1_FULL_SETTINGS', FULL_SETTINGS))
        with SSHConnection(args) as connection:
            try:
                if args.check:
                    result = stream_remote(connection.remote_command(full, '--check'), 'R1_FULL_CHECK')
                    if result.get('ready') is not True:
                        raise FullEntryError('只读检查未就绪；未启动服务或运动')
                    print('只读检查通过；未启动采集、展示或运动。', flush=True)
                    return 0
                measured = measure_robot_clock(args.robot_ip, args.robot_user, args.ssh_port,
                                               ssh_control_path=connection.control_path)
                offset = measured['offset_s']
                print('本次显示时差校正：%+.6f 秒。' % offset, flush=True)
                try:
                    prepared = stream_remote(connection.remote_command(full, '--prepare'), 'R1_FULL_PREPARED')
                except RemoteCancelled as exc:
                    if (isinstance(exc.result, dict) and isinstance(exc.result.get('token'), str)
                            and re.fullmatch(r'[0-9a-f]{32}', exc.result['token'])):
                        prepared = exc.result
                    raise
                token = prepared.get('token')
                if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{32}', token):
                    prepared = None
                    raise FullEntryError('远端准备结果缺少有效会话 token')
                model, services = prepared.get('model'), prepared.get('services')
                active = prepared.get('existing_controller')
                if (not isinstance(model, dict) or model.get('verified') is not True
                        or model.get('status') != 'ready' or prepared.get('services_ready') is not True
                        or not isinstance(active, list)
                        or any(type(pid) is not int or pid <= 0 for pid in active)
                        or not isinstance(services, dict) or not isinstance(services.get('vla'), dict)):
                    raise FullEntryError('远端准备结果未确认模型身份和采集就绪；未执行运动')
                vla = services['vla']
                source_id, robot_id = vla.get('source_id'), vla.get('robot_id')
                if not all(isinstance(item, str) and item for item in (source_id, robot_id)):
                    raise FullEntryError('远端准备结果缺少 VLA 采集源及机器人身份')
                host = '[%s]' % args.robot_ip if ':' in args.robot_ip else args.robot_ip
                camera = json.loads(live_launcher.fetch_snapshot(
                    'http://%s:%d/snapshot' % (host, args.robot_port), robot_clock_offset=offset))
                if robot_id is not None and camera.get('robot_id') != robot_id:
                    raise FullEntryError('相机与 VLA 采集机器人身份不一致；未执行运动')
                since = time.time()
                display = start_display(args, offset)
                wait_display(args, display, source_id, robot_id, since)
                url = 'http://127.0.0.1:%d/?source=live' % args.port
                print('完整演示展示：' + url, flush=True)
                if not args.no_browser:
                    open_browser(url, args.port)
                if prepared.get('existing_controller'):
                    return observe_existing(display)
                print('观察链路已就绪；现在执行一次原 start.sh --enable-motion。', flush=True)
                unknown_motion = True
                result = stream_remote(connection.remote_command(full, '--run', token), 'R1_FULL_RESULT', motion=True,
                    cancel_requested=lambda: display is not None and display.poll() is not None, token=token)
                if result.get('token') != token:
                    raise FullEntryError('远端返回会话身份不符；机器人状态未知')
                unknown_motion = False
                if result.get('status') == 'existing_controller':
                    if result.get('_local_cancelled'):
                        return 0
                    return observe_existing(display)
                if result.get('status') == 'failed':
                    print('原控制流程报告失败，但已确认停止；请查看上方远端日志。', file=sys.stderr, flush=True)
                    return 1
                print('远端原控制流程已正常结束并确认停止。', flush=True)
                return 0
            finally:
                # A helper release is guarded remotely against active motion.
                # Do not even ask it to release after an unconfirmed SSH loss.
                if prepared is not None and not unknown_motion:
                    try:
                        stream_remote(connection.remote_command(full, '--release', prepared['token']), 'R1_FULL_RELEASED')
                    except (FullEntryError, OSError, KeyboardInterrupt) as exc:
                        print('本次远端资源未确认释放：%s' % exc, file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        if unknown_motion:
            print('\n机器人状态未知：未收到本次控制流程的停止确认。', file=sys.stderr, flush=True)
            return 1
        print('\n已取消本次本机启动；已有控制和采集进程保持原归属。', flush=True)
        return 0
    except (EntryError, FullEntryError, ClockProbeError, live_launcher.LiveError, OSError) as exc:
        print('完整演示启动失败：%s' % exc, file=sys.stderr, flush=True)
        return 1
    finally:
        stop_owned_server(display)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    sys.exit(main())
