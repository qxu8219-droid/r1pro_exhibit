"""Read-only VLA log adapter and cursor HTTP service. Never imports robot code."""
import argparse
import base64
import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import struct
import threading
import time
from urllib.parse import parse_qs,urlsplit
import uuid
import zlib
import zipfile

MAX_LINE=3*1024*1024
MAX_RESPONSE=7*1024*1024


def contained(root, path):
    root=Path(root).resolve(); result=(root/Path(path)).resolve()
    if result!=root and root not in result.parents: raise ValueError('path escapes allowed root')
    return result


def atomic_json(path,value):
    temp=path.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(value,f,separators=(',',':'));f.flush();os.fsync(f.fileno())
    os.replace(temp,path)


def png(array):
    """Encode the actual uint8 RGB array; no external image package required."""
    import numpy as np
    a=np.asarray(array)
    if a.ndim==3 and a.shape[0]==3 and a.shape[2]!=3:a=a.transpose(1,2,0)
    if a.dtype!=np.uint8 or a.ndim!=3 or a.shape[2]!=3: raise ValueError('expected uint8 HWC RGB')
    if a.shape[0]>2048 or a.shape[1]>2048: raise ValueError('image too large')
    def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    raw=b''.join(b'\0'+row.tobytes() for row in a)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',a.shape[1],a.shape[0],8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw,3))+chunk(b'IEND',b'')


def read_archive(file, require_input=False):
    """Load only a complete bounded NPZ, using actual archived RGB and state."""
    import numpy as np
    stat=file.stat()
    if stat.st_size>24*1024*1024:raise ValueError('archive exceeds size limit')
    with zipfile.ZipFile(file) as archive:
        if sum(info.file_size for info in archive.infolist())>32*1024*1024:raise ValueError('expanded archive exceeds limit')
    images={};state=None;total=0
    with np.load(file,allow_pickle=False) as values:
        if require_input:
            state=np.asarray(values['state'])
            if state.shape!=(23,) or not np.isfinite(state).all():raise ValueError('state must have 23 finite numbers')
            state=state.tolist()
        for name in ('head_rgb','left_wrist_rgb','right_wrist_rgb'):
            if name in values:
                encoded=base64.b64encode(png(values[name])).decode();total+=len(encoded)
                if len(encoded)>700000 or total>2*1024*1024:raise ValueError('images exceed response allowance')
                images[name]='data:image/png;base64,'+encoded
    if not images or (require_input and len(images)!=3):raise ValueError('archive RGB inputs incomplete')
    after=file.stat()
    if (stat.st_size,stat.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('archive changed during read')
    return state,images,stat.st_mtime


class Collector:
    def __init__(self,spool,telemetry_file=None,legacy_root=None,robot_id='robot239'):
        self.robot_id=robot_id
        self.root=Path(spool);self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'events.jsonl';self.meta=self.root/'state.json'
        self.telemetry_file=Path(telemetry_file) if telemetry_file else None
        self.legacy_root=Path(legacy_root).resolve() if legacy_root else None
        self.lock=threading.RLock();self.ids=set();self.pending={};self.error=None
        try:self.state=json.loads(self.meta.read_text())
        except (OSError,ValueError):self.state={}
        size=self.path.stat().st_size if self.path.exists() else 0
        if size<self.state.get('size',0) or (self.path.exists() and self.state.get('inode') not in (None,self.path.stat().st_ino)):
            self.state={}
        self.source_id=self.state.setdefault('source_id',uuid.uuid4().hex)
        self.state.setdefault('hooked_sessions',[]);self.state.setdefault('offsets',{});self.state.setdefault('operations',{});self.state.setdefault('anchors',{})
        self.state.setdefault('anchor_trusted',{});self.state.setdefault('dropped',0);self.state.setdefault('observed_at',None)
        self.pending=dict(self.state.get('pending',{}))
        self.path.touch(exist_ok=True)
        # A torn append is never offered as an event, and is removed before append.
        good=0
        with self.path.open('rb') as f:
            while True:
                line=f.readline(MAX_LINE+1)
                if not line:break
                if not line.endswith(b'\n'):break
                good=f.tell()
                try:
                    event=json.loads(line);self.ids.add(event['event_id']);self._queue_artifact(event)
                    self.pending.pop(event['event_id'],None)
                except (ValueError,KeyError):pass
        if good<self.path.stat().st_size:
            with self.path.open('r+b') as f:f.truncate(good)
        self.pending={key:value for key,value in self.pending.items() if key not in self.ids}
        self._save()
    def _save(self):
        with self.path.open('ab') as f:f.flush();os.fsync(f.fileno())
        self.state.update(size=self.path.stat().st_size,inode=self.path.stat().st_ino,pending=self.pending)
        atomic_json(self.meta,self.state)
    def _check_spool(self):
        stat=self.path.stat()
        if stat.st_size<self.state.get('size',0) or stat.st_ino!=self.state.get('inode',stat.st_ino):
            self.source_id=uuid.uuid4().hex;self.state['source_id']=self.source_id
            self.state['offsets']={};self.ids=set();self.pending={}
            with self.path.open('rb') as f:
                for line in f:
                    try:self.ids.add(json.loads(line)['event_id'])
                    except (ValueError,KeyError):pass
            self._save()
    def append(self,event,persist=True):
        with self.lock:
            if event['event_id'] in self.ids:return
            data=(json.dumps(event,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n').encode()
            if len(data)>MAX_LINE:self.state['dropped']+=1;return
            with self.path.open('ab') as f:f.write(data)
            self.ids.add(event['event_id'])
            if persist:self._save()
    def _source(self,path,legacy):
        key=str(path.resolve()); stat=path.stat(); record=self.state['offsets'].get(key,{})
        offset=record.get('offset',0);generation=record.get('generation',0)
        changed=False
        if record.get('tail_hash') and offset<=stat.st_size:
            with path.open('rb') as check:
                check.seek(max(0,offset-128));changed=hashlib.sha256(check.read(min(offset,128))).hexdigest()!=record['tail_hash']
        if record and (record.get('inode')!=stat.st_ino or offset>stat.st_size or changed):
            offset=0;generation+=1
            self.state['operations'].pop(path.parent.name,None);self.state['anchors'].pop(path.parent.name,None)
            self.state['anchor_trusted'].pop(path.parent.name,None)
        with path.open('rb') as f:
            f.seek(offset)
            for _ in range(500):
                if time.monotonic()>getattr(self,'_deadline',float('inf')):break
                start=f.tell();line=f.readline(MAX_LINE+1)
                if not line or not line.endswith(b'\n'):break
                offset=f.tell()
                try:
                    raw=json.loads(line)
                    if legacy:event=self._legacy(raw,path,start,stat,generation)
                    else:
                        event=raw
                        session=raw.get('session_id')
                        if session and session not in self.state['hooked_sessions']:self.state['hooked_sessions'].append(session)
                    self.append(event,persist=False)
                    self._queue_artifact(event)
                    if stat.st_mtime >= (self.state.get('observed_at') or 0):self.state['current_legacy']=legacy
                    self.state['observed_at']=max(self.state.get('observed_at') or 0,stat.st_mtime)
                except (ValueError,KeyError,TypeError):self.state['dropped']+=1
        with path.open('rb') as check:
            check.seek(max(0,offset-128));tail_hash=hashlib.sha256(check.read(min(offset,128))).hexdigest()
        self.state['offsets'][key]=dict(offset=offset,inode=stat.st_ino,generation=generation,tail_hash=tail_hash)
        return offset>=stat.st_size
    def _legacy(self,raw,path,offset,stat,generation=0):
        session=path.parent.name;op=raw.get('operation')
        if isinstance(op,str) and '/' not in op and '..' not in op:self.state['operations'][session]=op
        op=self.state['operations'].get(session,'unknown')
        mono=raw.get('monotonic');stamps=raw.get('stamps') or []
        def clock_number(value):
            return type(value) in (int,float) and math.isfinite(value)
        # Feedback ROS stamps describe source measurements, whereas their paired
        # monotonic values describe receipt: transport jitter is not clock drift.
        # Use those only once as a fallback. A real snapshot clock pair upgrades
        # it once, then remains fixed for the session/source generation.
        if session not in self.state['anchors']:
            for i in range(0,len(stamps)-1,2):
                if clock_number(stamps[i]) and clock_number(stamps[i+1]) and stamps[i+1]>1000000000:
                    self.state['anchors'][session]=stamps[i+1]-stamps[i];break
        timing=(raw.get('observation_meta') or {}).get('timing') or {}
        if (not self.state['anchor_trusted'].get(session)
                and clock_number(timing.get('snapshot_ros_time'))
                and clock_number(timing.get('snapshot_monotonic'))):
            self.state['anchors'][session]=timing['snapshot_ros_time']-timing['snapshot_monotonic']
            self.state['anchor_trusted'][session]=True
        anchor=self.state['anchors'].get(session)
        observed=mono+anchor if isinstance(mono,(float,int)) and anchor else stat.st_mtime
        payload=dict(raw,source_generation=generation,legacy=True,lifecycle_known=False,source_mtime=stat.st_mtime)
        payload.pop('event',None)
        kind=raw.get('event','diagnostic');attempt=(raw.get('request_timing') or {}).get('request')
        if kind=='policy_rejected':kind='request_failed'
        event=dict(schema_version=1,event_id='legacy:'+hashlib.sha256((str(path)+':'+str(stat.st_ino)+':'+str(generation)+':'+str(offset)).encode()).hexdigest(),
                   robot_id=self.robot_id,session_id=session,run_id='legacy:'+session+(':'+str(generation) if generation else '')+':'+op,operation_id=op,
                   kind=kind,observed_at=observed,payload=payload)
        if mono is not None:event['monotonic']=mono
        if attempt is not None:
            payload['attempt']=attempt;payload['archive_request']=attempt
            payload['archive']=str(path.parent/op);event['request_id']=op+':request:'+str(attempt)
            if anchor and isinstance(raw.get('capture_monotonic'),(float,int)):payload['capture_at']=raw['capture_monotonic']+anchor
        return event
    def _artifact_key(self,event):
        n=event.get('payload',{}).get('archive_request')
        return 'artifact:'+hashlib.sha256((':'.join(str(event.get(k,'')) for k in ('session_id','run_id','operation_id','request_id'))+':'+str(n)).encode()).hexdigest()
    def _archive_observations(self,session):
        # An explicit source operation is required; a directory alone is not lifecycle evidence.
        op=self.state['operations'].get(session.name)
        if not op or not op.endswith('_run'):return
        archive=contained(self.legacy_root,session/op)
        generation=self.state['offsets'].get(str((session/'events.jsonl').resolve()),{}).get('generation',0)
        run='legacy:'+session.name+(':'+str(generation) if generation else '')+':'+op
        waits=self.state.setdefault('archive_wait',{});count=0
        for file in sorted(archive.glob('request_*_obs.npz'),reverse=True):
            try:n=int(file.name[len('request_'):-len('_obs.npz')])
            except ValueError:continue
            key='archive-observation:'+hashlib.sha256((run+':'+str(n)).encode()).hexdigest()
            if key in self.ids:continue
            if count>=4:break
            count+=1
            event=dict(schema_version=1,event_id=key,robot_id=self.robot_id,session_id=session.name,
                       run_id=run,operation_id=op,request_id=op+':request:'+str(n),kind='observation')
            try:
                file=contained(self.legacy_root,file)
                state,images,mtime=read_archive(file,require_input=True)
            except Exception as exc:
                started=waits.setdefault(key,time.time())
                if time.time()-started>=30 and key+':gap' not in self.ids:
                    self.state['dropped']+=1
                    gap=dict(event,event_id=key+':gap',kind='capture_gap',observed_at=file.stat().st_mtime,
                             payload=dict(reason='archive_input_unreadable: '+str(exc),attempt=n,legacy=True))
                    self.append(gap,persist=False)
                continue
            event.update(observed_at=mtime,payload=dict(attempt=n,state23=state,capture_at=None,
                observation_meta={'status':'archive_ready'},legacy=True,lifecycle_known=False,
                time_semantics='archive_file_mtime',source_generation=generation,archive=str(archive),archive_request=n))
            self.append(event,persist=False)
            artifact=dict(event,event_id=self._artifact_key(event),kind='artifact_ready',
                          payload=dict(attempt=n,images=images,status='ready',legacy=True,artifact_generated_at=time.time()))
            self.append(artifact,persist=False);self.pending.pop(artifact['event_id'],None);waits.pop(key,None)
            if mtime>=(self.state.get('observed_at') or 0):self.state['current_legacy']=True
            self.state['observed_at']=max(self.state.get('observed_at') or 0,mtime)
    def _queue_artifact(self,event):
        p=event.get('payload',{});n=p.get('archive_request')
        if n is None or not p.get('archive') or event.get('kind') not in ('request_started','raw_response','request_failed'):return
        key=self._artifact_key(event)
        if key not in self.ids and key not in self.pending:self.pending[key]=(event,time.time())
    def _artifacts(self):
        for key,(event,started) in list(self.pending.items())[:4]:
            p=event['payload']; images={};error=None
            try:
                if self.legacy_root is None:raise ValueError('archive root not configured')
                archive=contained(self.legacy_root,p['archive']);n=int(p['archive_request'])
                file=contained(archive,'request_%04d_obs.npz'%n)
                _,images,_=read_archive(file)
            except Exception as exc:
                error=type(exc).__name__+': '+str(exc)
                if time.time()-started<30:continue
            result=dict(event,event_id=key,kind='artifact_ready',observed_at=event.get('observed_at',time.time()),payload=dict(artifact_generated_at=time.time(),attempt=p.get('attempt'),images=images if not error else {},status='missing' if error else 'ready',reason=error,legacy=p.get('legacy',False)))
            self.append(result,persist=False);del self.pending[key]
    def poll(self):
        # Source parsing, NPZ decompression and metadata fsync never hold the HTTP lock.
        with self.lock:self._check_spool()
        self._deadline=time.monotonic()+.20
        try:
            if self.telemetry_file and self.telemetry_file.exists():self._source(self.telemetry_file,False)
            if self.legacy_root:
                candidates=[p for p in self.legacy_root.glob('client*/events.jsonl') if not p.is_symlink()]
                candidates.sort(key=lambda p:p.stat().st_mtime)
                self.state['legacy_selected']=[str(p.parent.name) for p in candidates[-1:]]
                for path in candidates[-1:]:
                    if path.resolve()==self.path.resolve() or (self.telemetry_file and path.resolve()==self.telemetry_file.resolve()):continue
                    if path.parent.name in self.state['hooked_sessions']:continue
                    done=self._source(path,True)
                    if done:
                        self._archive_observations(path.parent)
                        self._summaries(path.parent)
            self._artifacts();self._save();self.error=None
        except Exception as exc:self.error=repr(exc)
    def _summaries(self,session):
        for path in sorted(session.glob('*/SUMMARY.json')):
            key='summary:'+hashlib.sha256(str(path).encode()).hexdigest()
            if key in self.ids:continue
            try:
                raw=json.loads(path.read_text());stamp=path.stat().st_mtime;op=path.parent.name
                if raw.get('finished_utc'):
                    stamp=datetime.datetime.fromisoformat(raw['finished_utc'].replace('Z','+00:00')).timestamp()
                event=dict(schema_version=1,event_id=key,robot_id=self.robot_id,session_id=session.name,
                    run_id='legacy:'+session.name+':'+op,operation_id=op,kind='operation_finished',observed_at=stamp,
                    payload=dict(mode=raw.get('mode'),result_kind=raw.get('event_kind'),result=raw.get('result',{}),archive=str(path.parent),legacy=True,lifecycle_known=False))
                self.append(event,persist=False)
            except (OSError,ValueError):continue
    def health(self):
        return dict(service='vla-evidence-observer',pid=os.getpid(),robot_id=self.robot_id,source_id=self.source_id,observed_at=self.state.get('observed_at'),dropped=self.state['dropped'],legacy=self.state.get('current_legacy',bool(self.legacy_root)),legacy_scope='latest_session',legacy_selected=self.state.get('legacy_selected',[]),error=self.error)
    def read(self,after,limit):
        with self.lock:
            self._check_spool()
            size=self.path.stat().st_size
            if after<0 or after>size:raise ValueError('cursor outside source')
            events=[];total=0;cursor=after
            with self.path.open('rb') as f:
                if after:
                    f.seek(after-1)
                    if f.read(1)!=b'\n':raise ValueError('cursor is not a line boundary')
                f.seek(after)
                for _ in range(min(max(limit,1),200)):
                    line=f.readline(MAX_LINE+1)
                    if not line or not line.endswith(b'\n') or total+len(line)>MAX_RESPONSE:break
                    events.append(json.loads(line));total+=len(line);cursor=f.tell()
            return dict(source_id=self.source_id,next_cursor=cursor,has_more=cursor<size,events=events,health=self.health())


def make_server(host,port,collector):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed=urlsplit(self.path);status=200
            try:
                if parsed.path=='/healthz':data=collector.health()
                elif parsed.path=='/events':
                    q=parse_qs(parsed.query);data=collector.read(int(q.get('after',['0'])[0]),int(q.get('limit',['200'])[0]))
                else:status=404;data={'error':'not found'}
            except (ValueError,TypeError):status=416;data={'error':'invalid cursor or limit','source_id':collector.source_id}
            body=json.dumps(data,separators=(',',':')).encode()
            self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    return ThreadingHTTPServer((host,port),Handler)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--spool',required=True);p.add_argument('--robot-id',default=os.environ.get('VLA_TELEMETRY_ROBOT_ID','robot239'));p.add_argument('--legacy-root');p.add_argument('--telemetry-file');p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=8774)
    args=p.parse_args();c=Collector(args.spool,args.telemetry_file,args.legacy_root,args.robot_id);stop=threading.Event()
    def collect():
        while not stop.is_set():c.poll();stop.wait(.5)
    thread=threading.Thread(target=collect,name='vla-log-reader',daemon=True);thread.start();server=make_server(args.host,args.port,c)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:stop.set();server.server_close();thread.join(2)

if __name__=='__main__':main()
