#!/usr/bin/env python3
"""Own one full-demo session without modifying the existing robot installation.

Deploy this file in its own full-demo directory. Model inference servers must
already be running. --check never creates files or starts services or motion.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import select
import termios
import signal
import socket
import subprocess
import sys
import time
import uuid


class SessionError(Exception):
    pass


def validate_config(config):
    required = ('coordinator_dir', 'robot_config', 'vla_python', 'services_script',
                'tunnel_host', 'tunnel_user', 'tunnel_local_port', 'tunnel_remote_port')
    if not isinstance(config, dict) or set(config) != set(required):
        raise SessionError('Configuration must contain exactly: ' + ', '.join(required))
    for key in ('coordinator_dir', 'robot_config', 'vla_python', 'services_script'):
        value = config[key]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise SessionError(key + ' must be an absolute path')
        if not Path(value).exists():
            raise SessionError(key + ' does not exist: ' + value)
    if not (Path(config['coordinator_dir']) / 'start.sh').is_file():
        raise SessionError('Original start.sh is missing')
    for key in ('tunnel_local_port', 'tunnel_remote_port'):
        if type(config[key]) is not int or not 1 <= config[key] <= 65535:
            raise SessionError(key + ' must be a valid integer port')
    for key in ('tunnel_host', 'tunnel_user'):
        if not isinstance(config[key], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', config[key]) or config[key].startswith('-'):
            raise SessionError('Invalid ' + key)
    return config


def is_controller(argv, config, cwd=None):
    """Match Python entrypoints, never strings inside python -c diagnostics."""
    if not argv or not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(argv[0]).name):
        return False
    index = 1
    while index < len(argv) and argv[index] in ('-B', '-u', '-E', '-s', '-S', '-I', '-O', '-OO'):
        index += 1
    if index >= len(argv) or argv[index] in ('-c', '-'):
        return False
    if argv[index] == '-m':
        return index + 1 < len(argv) and argv[index + 1] in (
            'flexible_coordinator.launcher', 'flexible_coordinator.executor')
    script = Path(argv[index])
    if not script.is_absolute() and cwd is not None:
        script = Path(cwd) / script
    return script.is_absolute() and script == Path(config['coordinator_dir']) / 'executor.py'


def controllers(config):
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = [part.decode(errors='replace') for part in (entry / 'cmdline').read_bytes().split(b'\0') if part]
            cwd = (entry / 'cwd').resolve()
            if is_controller(argv, config, cwd):
                found.append(int(entry.name))
        except (OSError, ValueError):
            continue
    return sorted(found)


def occupied(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.5):
            return True
    except OSError:
        return False


def safe_root(root):
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
        raise SessionError('Session root must be an absolute directory without symlinks')
    if not root.is_dir():
        raise SessionError('Session root must already exist')
    return root


def read_json(path):
    try:
        with os.fdopen(os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW), 'r') as stream:
            return json.load(stream)
    except (OSError, ValueError) as error:
        raise SessionError('Cannot read session file: ' + str(path)) from error


def load_run(root, token):
    root = safe_root(root)
    if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
        raise SessionError('Invalid session token')
    runs = root / 'runs'
    run = runs / token
    if runs.is_symlink() or run.is_symlink() or not run.is_dir() or run.resolve() != run:
        raise SessionError('Session token does not identify an owned run directory')
    state = read_json(run / 'state.json')
    if not isinstance(state, dict) or state.get('token') != token:
        raise SessionError('Session token does not match saved state')
    config = validate_config(read_json(run / 'config.json'))
    return run, config, state


def check(root, config):
    safe_root(root)
    validate_config(config)
    active = controllers(config)
    model = {'status': 'unavailable'}
    if occupied(config['tunnel_local_port']):
        model = probe_model(config)
    return dict(ready=model.get('status') == 'ready', existing_controller=active,
                model=model, tunnel={'port': config['tunnel_local_port'], 'owned': False})


MODEL_HELLO = r"""
import json, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from g05_msgpack import unpackb
from websockets.sync.client import connect
with connect(sys.argv[2], open_timeout=5, close_timeout=1, max_size=1048576) as connection:
    hello = unpackb(connection.recv(timeout=5))
print(json.dumps(hello, separators=(',', ':')))
"""


def probe_model(config):
    robot = read_json(Path(config['robot_config']))
    try:
        bundle = Path(config['coordinator_dir']) / robot['client_bundle']
        manifest = read_json(bundle / robot['manifest'])
    except (KeyError, TypeError) as error:
        raise SessionError('Robot configuration lacks client_bundle/manifest') from error
    if not isinstance(manifest, dict):
        raise SessionError('Model manifest must be a JSON object')
    required = ['protocol', 'model_id', 'checkpoint_sha256', 'horizon', 'fps', 'mode']
    if manifest.get('server_optimization_id') is not None:
        required.append('server_optimization_id')
    if manifest.get('inference_variant') is not None:
        required.extend(('inference_variant', 'fm_steps', 'websocket_compression'))
    if any(key not in manifest for key in required):
        raise SessionError('Model manifest is missing identity fields')
    result = subprocess.run([config['vla_python'], '-B', '-c', MODEL_HELLO,
                             str(bundle / 'camera_runtime'),
                             'ws://127.0.0.1:%d' % config['tunnel_local_port']],
                            cwd=config['coordinator_dir'],
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=13)
    if result.returncode:
        raise SessionError('Model WebSocket hello failed: ' + result.stderr.strip()[-1500:])
    try:
        hello = json.loads(result.stdout)
    except ValueError as error:
        raise SessionError('Model WebSocket hello was not valid metadata') from error
    if not isinstance(hello, dict):
        raise SessionError('Model WebSocket hello was not a metadata object')
    for key in required:
        if key not in hello or hello[key] != manifest[key]:
            raise SessionError('Model hello identity mismatch: ' + key)
    return dict(status='ready', verified=True, **{key: hello[key] for key in required})


def write_json(path, value):
    if path.is_symlink():
        raise SessionError('Refusing symlink session file: ' + str(path))
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    with os.fdopen(os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as stream:
        json.dump(value, stream, separators=(',', ':'))
        stream.write('\n')
    os.replace(str(temporary), str(path))


def lock_file(root, name):
    stream = os.fdopen(os.open(str(root / name), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'a')
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        stream.close()
        raise SessionError('Another full-demo session holds ' + name) from error
    return stream


def close_tunnel(run, config, state):
    tunnel = state['tunnel']
    if (run / 'ssh.sock').is_symlink():
        raise SessionError('Refusing symlink tunnel control socket')
    if not tunnel.get('owned') or tunnel.get('closed'):
        return False
    result = subprocess.run(['ssh', '-S', str(run / 'ssh.sock'), '-O', 'exit',
                             config['tunnel_user'] + '@' + config['tunnel_host']],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=10)
    if result.returncode:
        state['release_error'] = result.stderr.strip() or 'SSH control socket did not confirm exit'
        write_json(run / 'state.json', state)
        return False
    tunnel['closed'] = True
    write_json(run / 'state.json', state)
    return True


def release(root, token):
    run, config, state = load_run(root, token)
    try:
        lock = lock_file(root, '.motion.lock')
    except SessionError:
        return dict(token=token, tunnel_closed=False, status='motion_active')
    with lock:
        if controllers(config):
            return dict(token=token, tunnel_closed=False, status='motion_active')
        return dict(token=token, tunnel_closed=close_tunnel(run, config, state), status='released')


def prepare(root, config):
    root = safe_root(root)
    validate_config(config)
    original = Path(config['coordinator_dir']).resolve()
    if root == original or original in root.parents or root in original.parents:
        raise SessionError('Full-demo outputs must be separate from the original coordinator')
    with lock_file(root, '.prepare.lock'):
        runs = root / 'runs'
        if runs.is_symlink():
            raise SessionError('Refusing symlink runs directory')
        runs.mkdir(mode=0o700, exist_ok=True)
        token = uuid.uuid4().hex
        run = runs / token
        run.mkdir(mode=0o700)
        state = dict(token=token, phase='preparing', tunnel={'owned': False, 'port': config['tunnel_local_port']})
        write_json(run / 'config.json', config)
        write_json(run / 'state.json', state)
        try:
            active = controllers(config)
            if not occupied(config['tunnel_local_port']):
                known_hosts = Path.home() / '.ssh' / 'known_hosts'
                data = known_hosts.read_bytes() if known_hosts.exists() else b''
                with os.fdopen(os.open(str(run / 'known_hosts'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                    stream.write(data)
                state['tunnel'].update(owned=True, socket=str(run / 'ssh.sock'))
                write_json(run / 'state.json', state)
                command = ['ssh', '-N', '-f', '-M', '-S', str(run / 'ssh.sock'),
                           '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15',
                           '-o', 'ServerAliveCountMax=3', '-o', 'StrictHostKeyChecking=ask',
                           '-o', 'UserKnownHostsFile=' + str(run / 'known_hosts'),
                           '-o', 'UpdateHostKeys=no', '-o', 'PermitLocalCommand=no',
                           '-L', '127.0.0.1:%d:127.0.0.1:%d' %
                           (config['tunnel_local_port'], config['tunnel_remote_port']),
                           config['tunnel_user'] + '@' + config['tunnel_host']]
                if subprocess.call(command, cwd=run) != 0:
                    raise SessionError('SSH tunnel could not be established')
            model = probe_model(config)
            if model.get('verified') is not True:
                raise SessionError('Model handshake did not verify the configured model')
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', VLA_PYTHON=config['vla_python'])
            services = subprocess.run([config['vla_python'], '-B', config['services_script'],
                                       '--root', str(run / 'services')], cwd=run, env=env,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, timeout=90)
            (run / 'services.log').write_text(services.stdout + services.stderr)
            try:
                report = json.loads(services.stdout)
            except ValueError as error:
                raise SessionError('Capture service launcher returned invalid JSON') from error
            if services.returncode or not isinstance(report, dict) or report.get('status') != 'ready':
                raise SessionError('Capture services are not ready; inspect ' + str(run / 'services.log'))
            write_json(run / 'services.json', report)
            service_records = report.get('services')
            if not isinstance(service_records, dict):
                raise SessionError('Capture service launcher did not return service identities')
            if 'vla' in service_records:
                service_records['vla']['robot_id'] = 'R1Pro2026_001'
            state.update(phase='prepared', existing_controller=active, model=model,
                         services=service_records, services_ready=True)
            write_json(run / 'state.json', state)
            return dict(token=token, existing_controller=active, model=model,
                        tunnel=state['tunnel'], services=service_records, services_ready=True)
        except BaseException:
            state['phase'] = 'prepare_failed'
            write_json(run / 'state.json', state)
            try:
                release(root, token)
            except (SessionError, OSError, subprocess.SubprocessError):
                pass
            raise


HEARTBEAT_TIMEOUT = 10.0


def emit(marker, report):
    try:
        prefix = '\n' if marker == 'R1_FULL_RESULT' else ''
        print(prefix + marker + ' ' + json.dumps(report, separators=(',', ':')), flush=True)
        return True
    except (BrokenPipeError, OSError):
        return False


def run_session(root, token):
    run, config, state = load_run(root, token)
    result = dict(token=token, status='unknown', returncode=None, stop_confirmed=False, owned=False)
    with lock_file(root, '.motion.lock'):
        # Reload under the lock: another invocation may have consumed this token.
        run, config, state = load_run(root, token)
        if state.get('phase') != 'prepared':
            raise SessionError('Session token was already consumed or is not prepared')
        state['phase'] = 'consumed'
        write_json(run / 'state.json', state)
        active = controllers(config) or state.get('existing_controller', [])
        if active:
            result.update(status='existing_controller', existing_controller=active)
            return result
        if probe_model(config).get('verified') is not True:
            raise SessionError('Model handshake was not verified immediately before motion')
        child = None
        cancelled = None
        signal_sent = False
        cancel_offset = 0
        log = run / 'motion.log'
        previous_handlers = {}
        terminal = None
        input_fd = sys.stdin.fileno()
        pending_input = b''
        last_heartbeat = None
        start_wait = time.monotonic()
        output_alive = True
        seen_stop = False

        def cancel(reason):
            nonlocal cancelled, cancel_offset, seen_stop, signal_sent
            if cancelled is None:
                cancelled = reason
                try:
                    cancel_offset = log.stat().st_size
                except FileNotFoundError:
                    cancel_offset = 0
                seen_stop = False
            if child is not None and not signal_sent and child.poll() is None:
                signal_sent = True
                try:
                    child.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass

        def signal_handler(signum, frame):
            cancel(signal.Signals(signum).name)

        def heartbeat():
            nonlocal pending_input, last_heartbeat
            readable, _, _ = select.select([input_fd], [], [], 0)
            if readable:
                chunk = os.read(input_fd, 4096)
                if not chunk:
                    cancel('heartbeat_eof')
                    return
                pending_input += chunk
                if len(pending_input) > 8192:
                    cancel('invalid_heartbeat')
                    return
                while b'\n' in pending_input:
                    line, pending_input = pending_input.split(b'\n', 1)
                    if line.rstrip(b'\r') == b'R1_FULL_HEARTBEAT':
                        last_heartbeat = time.monotonic()
            if time.monotonic() - (last_heartbeat or start_wait) > HEARTBEAT_TIMEOUT:
                cancel('heartbeat_timeout')

        try:
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous_handlers[signum] = signal.signal(signum, signal_handler)
            if os.isatty(input_fd):
                terminal = termios.tcgetattr(input_fd)
                changed = list(terminal)
                changed[3] &= ~termios.ECHO
                termios.tcsetattr(input_fd, termios.TCSANOW, changed)
            if not emit('R1_FULL_ARMED', dict(token=token, heartbeat_timeout=HEARTBEAT_TIMEOUT)):
                cancel('output_disconnected')
            while last_heartbeat is None and cancelled is None:
                heartbeat()
                time.sleep(.05)
            if cancelled is None:
                env = dict(os.environ, B041_POLICY_URL='ws://127.0.0.1:%d' % config['tunnel_local_port'],
                           VLA_PYTHON=config['vla_python'], ROBOT_CONFIG=config['robot_config'],
                           PYTHONDONTWRITEBYTECODE='1')
                with log.open('xb', buffering=0) as output:
                    child = subprocess.Popen(['bash', 'start.sh', '--enable-motion'],
                                             cwd=config['coordinator_dir'], env=env,
                                             stdin=subprocess.DEVNULL, stdout=output,
                                             stderr=subprocess.STDOUT, start_new_session=True)
                result['owned'] = True
                if cancelled is not None:
                    cancel(cancelled)
                state.update(phase='running', child_pid=child.pid)
                write_json(run / 'state.json', state)
                last_port_check = time.monotonic()
                with log.open('rb') as tail:
                    while True:
                        data = tail.read(65536)
                        if data:
                            if output_alive:
                                try:
                                    sys.stdout.write(data.decode(errors='replace'))
                                    sys.stdout.flush()
                                except (BrokenPipeError, OSError):
                                    output_alive = False
                                    cancel('output_disconnected')
                        if child.poll() is not None:
                            # One last drain covers bytes written just before exit.
                            remainder = tail.read()
                            if remainder and output_alive:
                                try:
                                    sys.stdout.write(remainder.decode(errors='replace'))
                                    sys.stdout.flush()
                                except (BrokenPipeError, OSError):
                                    output_alive = False
                            break
                        heartbeat()
                        if cancelled is None and time.monotonic() - last_port_check >= 1:
                            last_port_check = time.monotonic()
                            if not occupied(config['tunnel_local_port']):
                                cancel('model_tunnel_lost')
                        time.sleep(.05)
        except BaseException as error:
            result['error'] = str(error)
            cancel('supervisor_error')
        finally:
            if child is not None:
                if child.poll() is None:
                    cancel('supervisor_exit')
                # No timeout and no escalation: the original launcher owns stop verification.
                result['returncode'] = child.wait()
                with log.open('rb') as stream:
                    if cancelled and cancel_offset:
                        stream.seek(cancel_offset - 1)
                        if stream.read(1) != b'\n':
                            stream.readline()  # A pre-cancel partial line cannot confirm a stop.
                    seen_stop = any(line.rstrip(b'\r\n') == b'Stop confirmed.' for line in stream)
                result['stop_confirmed'] = result['returncode'] == 0 and seen_stop
            if terminal is not None:
                try:
                    termios.tcsetattr(input_fd, termios.TCSANOW, terminal)
                except (OSError, termios.error):
                    pass
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            result['cancel_reason'] = cancelled
            result['status'] = ('cancelled' if cancelled and result['returncode'] in (None, 0)
                                else 'completed' if result['returncode'] == 0 else 'failed')
            state.update(phase='finished', result=result)
            write_json(run / 'state.json', state)
    released = release(root, token)
    result['tunnel_closed'] = released['tunnel_closed']
    write_json(run / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-json')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true')
    action.add_argument('--prepare', action='store_true')
    action.add_argument('--run', metavar='TOKEN')
    action.add_argument('--release', metavar='TOKEN')
    args = parser.parse_args(argv)
    root = Path(__file__).absolute().parent
    marker = ('R1_FULL_CHECK' if args.check else 'R1_FULL_PREPARED' if args.prepare
              else 'R1_FULL_RESULT' if args.run else 'R1_FULL_RELEASED')
    try:
        if args.check or args.prepare:
            if args.config_json is None:
                raise SessionError('--config-json is required for check/prepare')
            config = validate_config(json.loads(args.config_json))
            report = check(root, config) if args.check else prepare(root, config)
        else:
            if args.config_json is not None:
                raise SessionError('run/release use only the saved session configuration')
            report = run_session(root, args.run) if args.run else release(root, args.release)
        emit(marker, report)
        if args.check:
            return 0 if report['ready'] else 1
        if args.run:
            return 0 if report['status'] in ('completed', 'cancelled', 'existing_controller') else 1
        return 0
    except (SessionError, OSError, ValueError, subprocess.SubprocessError) as error:
        report = dict(status='error', error=str(error), ready=False)
        if args.run:
            report.update(token=args.run, returncode=None, stop_confirmed=False, owned=False)
            try:
                report['tunnel_closed'] = release(root, args.run)['tunnel_closed']
            except (SessionError, OSError, subprocess.SubprocessError):
                pass
        emit(marker, report)
        return 1


if __name__ == '__main__':
    sys.exit(main())
