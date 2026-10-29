#!/usr/bin/env python3
"""Start or reuse only the camera bridge and read-only VLA evidence observer.

Deploy alongside bridge.py and start_observer.sh in a new auto-services folder.
stdout is one JSON report; exit 0 means both requested HTTP identities were
verified (including camera images). It does not attest to frame freshness or
robot motion. Nonzero reports distinguish conflict, waiting, and error. Existing
listeners and newly launched children never receive a signal from this script.
"""
import argparse
import datetime
import fcntl
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import ProxyHandler, build_opener


CAMERA_SCRIPT = '/home/nvidia/r1pro_ws/vision_event_capture/live_stream/run_live_bridge.sh'
LEGACY_ROOT = '/home/nvidia/deformable-objects/robot_home/flexible_coordinator/runtime'
PYTHON = '/home/nvidia/vla_deploy/venv/bin/python3'
ROBOT_ID = 'R1Pro2026_001'
HTTP = build_opener(ProxyHandler({}))


class LaunchError(Exception):
    def __init__(self, message, status='error'):
        super().__init__(message)
        self.status = status


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def get_json(port, route, limit):
    with HTTP.open('http://127.0.0.1:%d%s' % (port, route), timeout=.35) as response:
        if response.status != 200:
            raise ValueError('unexpected HTTP status')
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError('HTTP response exceeds size limit')
        return json.loads(body)


def occupied(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.35):
            return True
    except OSError:
        return False


def probe(service, port, robot):
    """Return free, ready, waiting (recognized warming camera), or conflict."""
    try:
        health = get_json(port, '/healthz', 65536)
    except (OSError, ValueError, HTTPException):
        return {'state': 'conflict' if occupied(port) else 'free'}
    if not isinstance(health, dict):
        return {'state': 'conflict'}
    if service == 'vla':
        valid = (health.get('service') == 'vla-evidence-observer'
                 and health.get('robot_id') == robot
                 and isinstance(health.get('source_id'), str) and health['source_id']
                 and type(health.get('pid')) is int and health['pid'] > 0)
        if valid:
            return dict(state='ready', pid=health['pid'], source_id=health['source_id'])
        return {'state': 'conflict'}
    valid = (health.get('ok') is True
             and type(health.get('version')) is int and health['version'] >= 0
             and number(health.get('updated')) and health['updated'] >= 0
             and type(health.get('bytes')) is int and health['bytes'] >= 0)
    if not valid:
        return {'state': 'conflict'}
    try:
        snapshot = get_json(port, '/snapshot', 8 * 1024 * 1024)
    except (OSError, ValueError, HTTPException):
        return {'state': 'conflict'}
    if snapshot == {}:
        return {'state': 'waiting'}
    if (not isinstance(snapshot, dict) or type(snapshot.get('schema_version')) is not int
            or snapshot.get('schema_version') != 1
            or snapshot.get('robot_id') != robot or not number(snapshot.get('observed_at'))):
        return {'state': 'conflict'}
    images = snapshot.get('images')
    if not isinstance(images, dict):
        return {'state': 'waiting'}
    for key in ('head', 'wrist_left', 'wrist_right'):
        image = images.get(key)
        if not (isinstance(image, str) and image.startswith('data:image/')
                and ';base64,' in image and image.split(';base64,', 1)[1]):
            return {'state': 'waiting'}
    return dict(state='ready', observed_at=snapshot['observed_at'])


def separate(root, original):
    if root == original or root in original.parents or original in root.parents:
        raise LaunchError('Output root must be separate from original source and log directories')


def directory(root, path):
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise LaunchError('Output path escapes the new service root: ' + str(path))
    path.mkdir(parents=True, exist_ok=True)
    return path


def child_environment(run):
    env = dict(os.environ)
    env.update(PYTHONDONTWRITEBYTECODE='1',
               XDG_CACHE_HOME=str(directory(run, run / 'cache')),
               ROS_LOG_DIR=str(directory(run, run / 'ros/log')),
               ROS_HOME=str(directory(run, run / 'ros/home')),
               GST_REGISTRY=str(run / 'cache/gstreamer-registry.bin'),
               TMPDIR=str(directory(run, run / 'tmp')))
    return env


def wait_ready(service, port, robot, timeout, record, process=None):
    deadline = time.monotonic() + timeout
    while True:
        state = probe(service, port, robot)
        if process is not None and process.poll() is not None:
            # start_observer.sh exits after its detached child is healthy.
            if service == 'camera' or process.returncode != 0:
                raise LaunchError('%s exited before healthy (exit %s); inspect %s' %
                                  (service, process.returncode, record.get('log')))
        if state['state'] == 'ready':
            record.update({key: value for key, value in state.items() if key != 'state'})
            return
        if state['state'] == 'conflict':
            raise LaunchError('%s port conflict on %d: listener identity could not be verified; left untouched' %
                              (service, port), 'conflict')
        if time.monotonic() >= deadline:
            record['status'] = 'waiting'
            raise LaunchError('%s on port %d is waiting for verified health/images; process left running; inspect %s' %
                              (service, port, record.get('log', 'the existing service logs')), 'waiting')
        time.sleep(min(.1, max(0, deadline - time.monotonic())))


def start_camera(args, run, script, record):
    camera = directory(run, run / 'camera')
    log = camera / 'camera.log'
    command = ['nohup', 'bash', str(script), '--output', str(camera / 'snapshot.json'),
               '--http-port', str(args.camera_port), '--robot-id', args.robot_id]
    with log.open('ab', buffering=0) as output:
        process = subprocess.Popen(command, cwd=camera, env=child_environment(run),
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True)
    (camera / 'camera.pid').write_text(str(process.pid) + '\n')
    record.update(status='started', pid=process.pid, log=str(log), output=str(camera / 'snapshot.json'))
    wait_ready('camera', args.camera_port, args.robot_id, args.startup_timeout, record, process)


def start_observer(args, run, bundle, legacy, record):
    vla = directory(run, run / 'vla')
    log = vla / 'launcher.log'
    env = child_environment(run)
    env.update(VLA_OBSERVER_ROOT=str(vla), VLA_OBSERVER_PORT=str(args.vla_port),
               VLA_OBSERVER_ROBOT_ID=args.robot_id, VLA_LEGACY_ROOT=str(legacy),
               VLA_PYTHON=os.environ.get('VLA_PYTHON', PYTHON))
    with log.open('ab', buffering=0) as output:
        process = subprocess.Popen(['nohup', 'bash', str(bundle / 'start_observer.sh')],
                                   cwd=vla, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    record.update(status='started', launcher_pid=process.pid, log=str(log))
    wait_ready('vla', args.vla_port, args.robot_id, args.startup_timeout, record, process)


def launch(args, report):
    bundle = Path(__file__).resolve().parent
    root = Path(args.root).resolve()
    camera_script = Path(os.environ.get('VLA_CAMERA_SCRIPT', CAMERA_SCRIPT)).resolve()
    legacy = Path(os.environ.get('VLA_LEGACY_ROOT', LEGACY_ROOT)).resolve()
    separate(root, legacy)
    separate(root, camera_script.parent)
    root.mkdir(parents=True, exist_ok=True)
    report['root'] = str(root)
    # A no-follow lock protects this output folder without opening symlink targets.
    lock_fd = os.open(str(root / '.start.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        requested = [('camera', args.camera_port)]
        if not args.camera_only:
            requested.append(('vla', args.vla_port))
        states = {name: probe(name, port, args.robot_id) for name, port in requested}
        for name, port in requested:
            record = report['services'][name] = dict(port=port, url='http://127.0.0.1:%d' % port)
            if states[name]['state'] == 'conflict':
                record['status'] = 'conflict'
                raise LaunchError('%s port conflict on %d: listener identity could not be verified; left untouched' %
                                  (name, port), 'conflict')
        # Validate all required inputs before starting either service.
        if states['camera']['state'] == 'free' and not camera_script.is_file():
            raise LaunchError('Existing camera launch script is missing: ' + str(camera_script))
        if not args.camera_only and states['vla']['state'] == 'free':
            if not legacy.is_dir() or not os.access(legacy, os.R_OK | os.X_OK):
                raise LaunchError('VLA_LEGACY_ROOT must be a readable existing log directory; no source directory will be created')
            if not all((bundle / name).is_file() for name in ('start_observer.sh', 'bridge.py')):
                raise LaunchError('start_observer.sh and bridge.py must be beside start_services.py')
        run = None
        for name, port in requested:
            record = report['services'][name]
            if states[name]['state'] != 'free':
                record['status'] = 'reused'
                wait_ready(name, port, args.robot_id, args.startup_timeout, record)
                continue
            if run is None:
                runs = directory(root, root / 'runs')
                stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
                run = Path(tempfile.mkdtemp(prefix=stamp, dir=runs))
                report['run'] = str(run)
            if name == 'camera':
                start_camera(args, run, camera_script, record)
            else:
                start_observer(args, run, bundle, legacy, record)
    report['status'] = 'ready'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(Path(__file__).resolve().parent))
    parser.add_argument('--camera-port', type=int, default=8772)
    parser.add_argument('--vla-port', type=int, default=8774)
    parser.add_argument('--camera-only', action='store_true')
    parser.add_argument('--robot-id', default=ROBOT_ID)
    parser.add_argument('--startup-timeout', type=float, default=20)
    args = parser.parse_args(argv)
    if (not all(1 <= port <= 65535 for port in (args.camera_port, args.vla_port))
            or (not args.camera_only and args.camera_port == args.vla_port)):
        parser.error('service ports must be distinct integers from 1 to 65535')
    if not math.isfinite(args.startup_timeout) or args.startup_timeout <= 0:
        parser.error('--startup-timeout must be a positive finite number')
    if not args.robot_id.strip():
        parser.error('--robot-id must not be empty')
    report = dict(status='error', services={})
    try:
        launch(args, report)
    except (LaunchError, OSError) as error:
        report['status'] = error.status if isinstance(error, LaunchError) else 'error'
        report['error'] = str(error)
        print(str(error), file=sys.stderr)
        print(json.dumps(report, separators=(',', ':')))
        return 1
    print(json.dumps(report, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
