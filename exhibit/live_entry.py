"""Saved one-click defaults plus a read-only clock check before the existing launcher."""
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import sys

import live_launcher
from clock_probe import ClockProbeError, measure_robot_clock
from robot_clock import robot_clock_offset

SETTINGS = Path(__file__).resolve().parents[1] / 'live_settings.json'
FIELDS = {'robot_ip', 'robot_user', 'ssh_port', 'port', 'robot_port', 'vla_port',
          'snapshot_file', 'db', 'robot_clock_offset', 'services_script'}


class EntryError(ValueError):
    pass


def load_settings(path):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        if len(raw) > 16384:
            raise ValueError('配置文件过大')
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) - FIELDS:
            raise ValueError('配置包含未知字段；不要在配置中保存密码')
        for key, item in value.items():
            if key.endswith('port'):
                if type(item) is not int or not 1 <= item <= 65535:
                    raise ValueError(key + ' 必须是有效端口')
            elif key == 'robot_clock_offset':
                if item != 'auto':
                    value[key] = robot_clock_offset(item)
            elif not isinstance(item, str) or not item.strip() or any(ord(c) < 32 for c in item):
                raise ValueError(key + ' 必须是非空文本')
        for key in ('snapshot_file', 'db'):
            if key in value:
                destination = Path(value[key]).expanduser()
                value[key] = str((path.parent / destination).resolve())
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise EntryError('读取启动配置失败：%s：%s' % (path, exc)) from exc


def prepare(argv, settings_path=SETTINGS):
    if '--help' in argv or '-h' in argv:
        parser = live_launcher.build_parser()
        parser.epilog = '一键启动读取 live_settings.json；无需参数。可用 R1_LIVE_SETTINGS 指定其他配置文件。'
        parser.parse_args(list(argv))
    settings = load_settings(settings_path)
    clock = settings.pop('robot_clock_offset', 'auto')
    base = [part for key, value in settings.items()
            for part in ('--' + key.replace('_', '-'), str(value))]
    parser = live_launcher.build_parser()
    parser.set_defaults(robot_clock_offset=None if clock == 'auto' else clock)
    args = parser.parse_args(base + list(argv))
    # Validate before any SSH, including options the existing launcher validates
    # after parsing. Command-line values take precedence over saved defaults.
    try:
        ipaddress.ip_address(args.robot_ip)
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', args.robot_user):
            raise ValueError('SSH 用户名无效')
        if not all(1 <= p <= 65535 for p in (args.port, args.robot_port, args.ssh_port, args.vla_port)):
            raise ValueError('端口必须在 1 到 65535 之间')
        if not math.isfinite(args.wait_seconds) or args.wait_seconds < 0:
            raise ValueError('等待时间必须是有限非负数')
    except ValueError as exc:
        raise EntryError(str(exc)) from exc
    offset = args.robot_clock_offset
    if offset is None:
        if args.check:
            offset = 0.0
            print('只读检查不执行 SSH；如两端有时差，请附加 --robot-clock-offset 秒数。', flush=True)
        elif args.no_ssh:
            raise EntryError('--no-ssh 禁止自动测时，请明确给定 --robot-clock-offset 秒数（时钟一致时为 0）。')
        else:
            print('读取机器人当前时间；如 SSH 提示密码，请输入机器人密码（不会保存）。', flush=True)
            try:
                result = measure_robot_clock(args.robot_ip, args.robot_user, args.ssh_port)
                offset = robot_clock_offset(result['offset_s'])
            except ClockProbeError as exc:
                raise EntryError('自动测时失败：%s。未启动展示；可检查 SSH，或手动指定 --robot-clock-offset。' % exc) from exc
            print('本次显示时差校正：%+.6f 秒；测时往返 %.1f ms。' % (offset, result['rtt_s'] * 1000), flush=True)
    return base + list(argv) + ['--robot-clock-offset', str(offset)]


def main(argv=None):
    previous_signals = {}

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    try:
        # Own the startup phase as well as the eventual launcher phase: closing
        # the terminal while SSH is authenticating must reap that child too.
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_signals[signum] = signal.signal(signum, interrupt)
        args = prepare(sys.argv[1:] if argv is None else argv,
                       os.environ.get('R1_LIVE_SETTINGS', str(SETTINGS)))
        return live_launcher.main(args)
    except EntryError as exc:
        print('一键启动失败：' + str(exc), file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print('\n已取消一键启动。', flush=True)
        return 0
    finally:
        for signum, previous in previous_signals.items():
            signal.signal(signum, previous)


if __name__ == '__main__':
    sys.exit(main())
