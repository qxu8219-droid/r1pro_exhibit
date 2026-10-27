#!/usr/bin/env bash
# Start only the read-only evidence bridge. Never execute coordinator/start.sh.
set -euo pipefail
observer_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
observer_python="${VLA_PYTHON:-/home/nvidia/vla_deploy/venv/bin/python3}"
export PYTHONDONTWRITEBYTECODE=1
exec "$observer_python" -B - "$observer_script_dir" <<'PY'
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

script_dir=Path(sys.argv[1]).resolve()
root=Path(os.environ.get('VLA_OBSERVER_ROOT',str(script_dir))).resolve()
try:
    port=int(os.environ.get('VLA_OBSERVER_PORT','8774'))
    if not 1<=port<=65535:raise ValueError()
except ValueError:
    print('VLA_OBSERVER_PORT must be an integer from 1 to 65535',file=sys.stderr);sys.exit(2)
robot=os.environ.get('VLA_OBSERVER_ROBOT_ID','R1Pro2026_001')
legacy_root=Path(os.environ.get('VLA_LEGACY_ROOT','/home/nvidia/deformable-objects/robot_home/flexible_coordinator/runtime')).resolve()
if not legacy_root.is_dir() or not os.access(legacy_root,os.R_OK|os.X_OK):
    print('VLA_LEGACY_ROOT must be a readable existing log directory; no source directory will be created',file=sys.stderr);sys.exit(2)
if root==legacy_root or root in legacy_root.parents or legacy_root in root.parents:
    print('Observer output folder must be separate from the original log directory',file=sys.stderr);sys.exit(2)
spool=root/'runtime';spool.mkdir(parents=True,exist_ok=True)
pidfile=spool/'observer.pid';logfile=spool/'observer.log'
url='http://127.0.0.1:%d/healthz'%port

def health():
    try:
        with urlopen(url,timeout=.35) as response:
            raw=response.read(65537)
            if len(raw)>65536:return None
            data=json.loads(raw)
        if (data.get('service')=='vla-evidence-observer' and data.get('robot_id')==robot
                and isinstance(data.get('source_id'),str) and data['source_id']
                and type(data.get('pid')) is int and data['pid']>0):return data
    except Exception:pass
    return None

def occupied():
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=.35):return True
    except OSError:return False

def record_pid(pid):
    temp=pidfile.with_suffix('.tmp');temp.write_text(str(pid)+'\n');os.replace(temp,pidfile)

# Serialize simultaneous launch requests. No existing process receives a signal.
with (spool/'.start.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    existing=health()
    if existing:
        record_pid(existing['pid'])
        print(json.dumps(dict(status='reused',pid=existing['pid'],port=port,source_id=existing['source_id'])));sys.exit(0)
    if occupied():
        print('Observer port conflict on %d: listener is not this robot\'s evidence bridge; left untouched'%port,file=sys.stderr);sys.exit(2)
    bridge=script_dir/'bridge.py'
    if not bridge.is_file():print('bridge.py is missing',file=sys.stderr);sys.exit(2)
    command=['nohup',sys.executable,'-B',str(bridge),'--spool',str(spool),'--legacy-root',str(legacy_root),
             '--robot-id',robot,'--host','0.0.0.0','--port',str(port)]
    with logfile.open('ab',buffering=0) as log:
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                                 start_new_session=True,close_fds=True)
    record_pid(process.pid)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        data=health()
        if data and data['pid']==process.pid:
            print(json.dumps(dict(status='started',pid=process.pid,port=port,source_id=data['source_id'],log=str(logfile))));sys.exit(0)
        if process.poll() is not None:
            if pidfile.exists() and pidfile.read_text().strip()==str(process.pid):pidfile.unlink()
            print('Observer exited before healthy; inspect '+str(logfile),file=sys.stderr);sys.exit(1)
        time.sleep(.1)
    print('Observer health check timed out after 5 seconds; process left running, PID %d; inspect %s'%(process.pid,logfile),file=sys.stderr)
    sys.exit(1)
PY
