import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen
from urllib.error import HTTPError
from robot_side import bridge

class BridgeTests(unittest.TestCase):
    def test_complete_lines_cursor_restart_and_http(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); source=p/'source.jsonl'
            event={'schema_version':1,'event_id':'x:1','kind':'status','payload':{},'run_id':'r','operation_id':'o'}
            source.write_text(json.dumps(event)+'\n'+json.dumps(dict(event,event_id='x:2')))
            collector=bridge.Collector(p/'spool',telemetry_file=source)
            collector.poll(); first=collector.read(0,200)
            self.assertEqual(len(first['events']),1)
            cursor=first['next_cursor']; sid=first['source_id']
            self.assertEqual(collector.read(cursor,200)['events'],[])
            with source.open('a') as f:f.write('\n')
            collector.poll(); self.assertEqual(len(collector.read(cursor,200)['events']),1)
            again=bridge.Collector(p/'spool',telemetry_file=source); again.poll()
            self.assertEqual(again.read(0,200)['source_id'],sid)
            self.assertEqual(len(again.read(0,200)['events']),2)
            server=bridge.make_server('127.0.0.1',0,again)
            th=threading.Thread(target=server.serve_forever,daemon=True); th.start()
            try:
                base='http://127.0.0.1:'+str(server.server_port)
                with urlopen(base+'/events?after=0&limit=1') as r: data=json.load(r)
                self.assertTrue(data['has_more']); self.assertEqual(len(data['events']),1)
                with self.assertRaises(HTTPError): urlopen(base+'/events?after=99999999')
            finally:server.shutdown();server.server_close();th.join()
    def test_legacy_operation_is_explicit_and_attempt_uses_request_not_chunk(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'runtime'; session=root/'client239_a'; op=session/'0002_run'; op.mkdir(parents=True)
            rows=[{'event':'recovery_feedback','operation':'0002_run','feedback23':[0]*23},
                  {'event':'raw_response','chunk':0,'request_timing':{'request':4},'response':{'action23':[[0]*23]}}]
            (session/'events.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in rows))
            c=bridge.Collector(Path(d)/'spool',legacy_root=root);c.poll()
            rows=c.read(0,200)['events']; raw=next(x for x in rows if x['kind']=='raw_response')
            self.assertEqual(raw['run_id'],'legacy:client239_a:0002_run')
            self.assertEqual(raw['payload']['attempt'],4)
            self.assertTrue(raw['payload']['legacy'])
            self.assertNotIn('request_started',[x['kind'] for x in rows])
    def test_containment(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError): bridge.contained(Path(d), '../outside')
    def test_spool_truncation_changes_identity(self):
        with tempfile.TemporaryDirectory() as d:
            c=bridge.Collector(Path(d));c.append({'event_id':'a','kind':'status'})
            sid=c.source_id;c.path.write_text('')
            restarted=bridge.Collector(Path(d))
            self.assertNotEqual(sid,restarted.source_id)

if __name__=='__main__':unittest.main()

class ArtifactTests(unittest.TestCase):
    def test_retry_incomplete_npz_after_restart_uses_request_index(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'runtime';session=root/'client239_a';op=session/'0002_run';op.mkdir(parents=True)
            rows=[{'event':'recovery_feedback','operation':'0002_run'}, {'event':'raw_response','chunk':0,'request_timing':{'request':4}}]
            (session/'events.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in rows))
            file=op/'request_0004_obs.npz';file.write_bytes(b'partial')
            c=bridge.Collector(Path(d)/'spool',legacy_root=root);c.poll()
            c=bridge.Collector(Path(d)/'spool',legacy_root=root)
            np.savez(file,head_rgb=np.zeros((2,2,3),dtype=np.uint8),left_wrist_rgb=np.ones((2,2,3),dtype=np.uint8),right_wrist_rgb=np.ones((2,2,3),dtype=np.uint8))
            c.poll();artifacts=[x for x in c.read(0,200)['events'] if x['kind']=='artifact_ready']
            self.assertEqual(len(artifacts),1)
            self.assertEqual(artifacts[0]['payload']['attempt'],4)
            self.assertEqual(len(artifacts[0]['payload']['images']),3)
    def test_native_session_not_double_imported_as_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);root=p/'runtime';session=root/'client239_a';session.mkdir(parents=True)
            (session/'events.jsonl').write_text('{"event":"target"}\n')
            source=p/'native.jsonl';source.write_text(json.dumps({'event_id':'native:1','session_id':'client239_a','kind':'operation_started','payload':{}})+'\n')
            c=bridge.Collector(p/'spool',source,root);c.poll()
            self.assertEqual(len(c.read(0,200)['events']),1)

class ContainmentTests(unittest.TestCase):
    def test_spool_under_runtime_never_imports_itself(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'native'/'events.jsonl';source.parent.mkdir()
            source.write_text(json.dumps({'event_id':'n:1','session_id':'s','kind':'status','payload':{}})+'\n')
            c=bridge.Collector(root/'spool',source,root,robot_id='R1Pro2026_001');c.poll();c.poll()
            self.assertEqual(len(c.read(0,200)['events']),1)

class RequestArtifactTests(unittest.TestCase):
    def test_images_available_while_infer_pending_and_not_duplicated_by_response(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);root=p/'runtime';op=root/'client239_a'/'0002_run';op.mkdir(parents=True)
            np.savez(op/'request_0000_obs.npz',head_rgb=np.zeros((2,2,3),dtype=np.uint8))
            e=dict(event_id='n:1',kind='request_started',session_id='client239_a',run_id='r',operation_id='o',request_id='o:request:0',payload={'archive':str(op),'archive_request':0,'attempt':0})
            src=p/'native.jsonl';src.write_text(json.dumps(e)+'\n');c=bridge.Collector(p/'spool',src,root);c.poll()
            self.assertEqual(len([e for e in c.read(0,200)['events'] if e['kind']=='artifact_ready']),1)
            with src.open('a') as f:f.write(json.dumps(dict(e,event_id='n:2',kind='raw_response'))+'\n')
            c.poll();self.assertEqual(len([e for e in c.read(0,200)['events'] if e['kind']=='artifact_ready']),1)

class DeploymentTests(unittest.TestCase):
    def test_actual_robot_chw_rgb_encoded_as_correct_dimensions(self):
        import numpy as np
        import struct
        image=bridge.png(np.zeros((3,4,5),dtype=np.uint8))
        self.assertEqual(struct.unpack('!2I',image[16:24]),(5,4))
    def test_default_legacy_scope_only_latest_session_and_bounded_batch(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'runtime'
            for name,stamp in [('client_old',1),('client_current',2)]:
                session=root/name;session.mkdir(parents=True)
                file=session/'events.jsonl';file.write_text('{"event":"target"}\n'*600);os.utime(file,(stamp,stamp))
            c=bridge.Collector(Path(d)/'spool',legacy_root=root);c.poll()
            events=c.read(0,200)['events']
            self.assertEqual({e['session_id'] for e in events},{'client_current'})
            self.assertLess(len(c.path.read_text().splitlines()),600)
            self.assertEqual(c.health()['legacy_scope'],'latest_session')
    def test_poll_does_not_lock_out_http_reader(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);source=p/'source.jsonl';source.write_text('')
            c=bridge.Collector(p/'spool',source)
            entered=threading.Event();release=threading.Event();finished=threading.Event()
            def slow(*args):entered.set();release.wait(2)
            with patch.object(c,'_source',side_effect=slow):
                worker=threading.Thread(target=c.poll);worker.start();self.assertTrue(entered.wait(1))
                reader=threading.Thread(target=lambda:(c.read(0,200),finished.set()));reader.start()
                try:self.assertTrue(finished.wait(.2))
                finally:release.set();worker.join();reader.join()

class LiveSpoolTests(unittest.TestCase):
    def test_live_spool_truncation_changes_source_id(self):
        with tempfile.TemporaryDirectory() as d:
            c=bridge.Collector(Path(d));c.append({'event_id':'x','kind':'status'})
            before=c.source_id;c.path.write_text('')
            data=c.read(0,200)
            self.assertNotEqual(data['source_id'],before)

class LegacyGenerationTests(unittest.TestCase):
    def test_same_inode_truncate_rewrite_is_not_deduplicated_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'runtime';session=root/'client239_a';session.mkdir(parents=True);file=session/'events.jsonl'
            file.write_text(json.dumps({'event':'recovery_feedback','operation':'0002_run','reason':'long old data'})+'\n')
            c=bridge.Collector(Path(d)/'spool',legacy_root=root);c.poll();inode=file.stat().st_ino
            file.write_text('{"event":"target"}\n');self.assertEqual(inode,file.stat().st_ino);c.poll()
            events=c.read(0,200)['events'];self.assertEqual(len(events),2)
            self.assertNotEqual(events[0]['event_id'],events[1]['event_id'])
            self.assertEqual(events[1]['operation_id'],'unknown')
            c=bridge.Collector(Path(d)/'spool',legacy_root=root);c.poll();self.assertEqual(len(c.read(0,200)['events']),2)

class PendingPersistenceTests(unittest.TestCase):
    def test_pending_artifact_older_than_2000_events_survives_restart_with_deadline(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'runtime';archive=root/'client_a'/'0001_run';archive.mkdir(parents=True)
            c=bridge.Collector(Path(d)/'spool',legacy_root=root)
            event=dict(event_id='r',kind='raw_response',session_id='s',run_id='r',operation_id='o',request_id='q',payload={'archive':str(archive),'archive_request':0,'attempt':0})
            c.append(event);c._queue_artifact(event)
            key=next(iter(c.pending));c.pending[key]=(event,time.time()-12)
            for i in range(2001):c.append({'event_id':'diag:'+str(i),'kind':'diagnostic'},persist=False)
            c._save();original=c.pending[key][1]
            restarted=bridge.Collector(Path(d)/'spool',legacy_root=root)
            self.assertIn(key,restarted.pending)
            self.assertEqual(restarted.pending[key][1],original)
            import numpy as np
            np.savez(archive/'request_0000_obs.npz',head_rgb=np.zeros((3,4,5),dtype=np.uint8))
            restarted.poll()
            self.assertNotIn(key,restarted.pending)
            self.assertIn(key,restarted.ids)

class CurrentProvenanceTests(unittest.TestCase):
    def test_new_legacy_source_is_marked_legacy_after_older_native_session(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);source=p/'native.jsonl'
            source.write_text(json.dumps({'event_id':'n:1','session_id':'old_native','kind':'heartbeat','payload':{}})+'\n');os.utime(source,(1,1))
            root=p/'runtime';session=root/'client_new';session.mkdir(parents=True);events=session/'events.jsonl'
            events.write_text('{"event":"target"}\n');os.utime(events,(2,2))
            c=bridge.Collector(p/'spool',source,root);c.poll()
            self.assertTrue(c.health()['legacy'])
    def test_same_size_source_rewrite_has_new_generation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'runtime';session=root/'client_a';session.mkdir(parents=True);file=session/'events.jsonl'
            file.write_text('{"event":"target","step":1}\n')
            c=bridge.Collector(Path(d)/'spool',legacy_root=root);c.poll()
            file.write_text('{"event":"target","step":2}\n');c.poll()
            self.assertEqual([e['payload']['step'] for e in c.read(0,200)['events']],[1,2])

class ArchiveObservationTests(unittest.TestCase):
    def fixture(self,d):
        root=Path(d)/'original';session=root/'client_a';op=session/'0002_run';op.mkdir(parents=True)
        (session/'events.jsonl').write_text('{"event":"recovery_feedback","operation":"0002_run"}\n')
        return root,session,op
    def archive(self,path,state=None):
        import numpy as np
        np.savez(path,state=np.arange(23) if state is None else state,
                 head_rgb=np.zeros((3,4,5),dtype=np.uint8),left_wrist_rgb=np.zeros((3,4,5),dtype=np.uint8),right_wrist_rgb=np.zeros((3,4,5),dtype=np.uint8))
    def test_complete_archive_exposes_input_before_response_without_claiming_request_started(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            root,session,op=self.fixture(d);file=op/'request_0004_obs.npz';self.archive(file);os.utime(file,(1234,1234))
            c=bridge.Collector(Path(d)/'observer',legacy_root=root);c.poll();events=c.read(0,200)['events']
            observations=[e for e in events if e['kind']=='observation'];self.assertEqual(len(observations),1)
            obs=observations[0];self.assertEqual(obs['observed_at'],1234);self.assertIsNone(obs['payload']['capture_at'])
            self.assertEqual(obs['payload']['state23'],list(range(23)));self.assertEqual(obs['payload']['attempt'],4)
            self.assertEqual(obs['payload']['observation_meta']['status'],'archive_ready')
            self.assertNotIn('request_started',[e['kind'] for e in events])
            artifact=next(e for e in events if e['kind']=='artifact_ready');self.assertEqual(artifact['request_id'],obs['request_id']);self.assertEqual(len(artifact['payload']['images']),3)
            c=bridge.Collector(Path(d)/'observer',legacy_root=root);c.poll();self.assertEqual(len(c.read(0,200)['events']),len(events))
    def test_half_archive_retries_and_unknown_operation_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as d:
            root,session,op=self.fixture(d);file=op/'request_0001_obs.npz';file.write_bytes(b'partial')
            c=bridge.Collector(Path(d)/'observer',legacy_root=root);c.poll();self.assertFalse(any(e['kind']=='observation' for e in c.read(0,200)['events']))
            self.archive(file);c.poll();self.assertTrue(any(e['kind']=='observation' for e in c.read(0,200)['events']))
        with tempfile.TemporaryDirectory() as d:
            root,session,op=self.fixture(d);(session/'events.jsonl').write_text('{"event":"status"}\n');self.archive(op/'request_0001_obs.npz')
            c=bridge.Collector(Path(d)/'observer',legacy_root=root);c.poll();self.assertFalse(any(e['kind']=='observation' for e in c.read(0,200)['events']))
    def test_new_operation_and_attempts_do_not_reuse_input_identity(self):
        with tempfile.TemporaryDirectory() as d:
            root,session,op=self.fixture(d);self.archive(op/'request_0000_obs.npz');self.archive(op/'request_0001_obs.npz')
            c=bridge.Collector(Path(d)/'observer',legacy_root=root);c.poll()
            new=session/'0003_run';new.mkdir();self.archive(new/'request_0000_obs.npz')
            with (session/'events.jsonl').open('a') as f:f.write('{"event":"recovery_feedback","operation":"0003_run"}\n')
            c.poll();rows=[e for e in c.read(0,200)['events'] if e['kind']=='observation']
            self.assertEqual(len(rows),3);self.assertEqual(len({e['request_id'] for e in rows}),3)
    def test_nonfinite_or_wrong_state_does_not_emit_observation(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            root,session,op=self.fixture(d);self.archive(op/'request_0000_obs.npz',state=np.full(23,np.nan))
            c=bridge.Collector(Path(d)/'observer',legacy_root=root);c.poll()
            self.assertFalse(any(e['kind']=='observation' for e in c.read(0,200)['events']))

class LegacyClockTests(unittest.TestCase):
    # Exact clock-pair structure/values from the real robot sample, with small payloads.
    def test_snapshot_anchor_survives_feedback_delay_and_keeps_sample_after_target(self):
        rows=[
            {'event':'recovery_feedback','operation':'0002_run','stamps':[3255.582505568,1793108823.9032218],'monotonic':3255.58452912,'feedback23':[0]*23},
            {'event':'raw_response','monotonic':3256.78,'observation_meta':{'timing':{'snapshot_monotonic':3255.834345024,'snapshot_ros_time':1793108824.1637979}},'request_timing':{'request':0}},
            {'event':'target','monotonic':3256.78993696,'target23':[1]*23,'feedback23':[0]*23},
            {'event':'recovery_feedback','operation':'0002_run','stamps':[3256.795756608,1793108825.1132143],'monotonic':3256.798092032,'feedback23':[2]*23},
        ]
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'source';session=root/'client_a';session.mkdir(parents=True)
            (session/'events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            c=bridge.Collector(Path(d)/'observer',legacy_root=root);c.poll();events=c.read(0,200)['events']
            target=next(e for e in events if e['kind']=='target');feedback=events[-1]
            self.assertGreater(feedback['observed_at'],target['observed_at'])
            self.assertAlmostEqual(feedback['observed_at']-target['observed_at'],3256.798092032-3256.78993696,places=6)
            self.assertEqual(c.state['anchors']['client_a'],1793108824.1637979-3255.834345024)
            c=bridge.Collector(Path(d)/'observer',legacy_root=root)
            with (session/'events.jsonl').open('a') as f:f.write(json.dumps({'event':'raw_response','monotonic':3257,'observation_meta':{'timing':{'snapshot_monotonic':3257,'snapshot_ros_time':1793108824}}})+'\n')
            c.poll();self.assertEqual(c.state['anchors']['client_a'],1793108824.1637979-3255.834345024)
    def test_feedback_fallback_anchor_is_stable_until_first_snapshot_and_resets_on_generation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'source';session=root/'client_a';session.mkdir(parents=True);file=session/'events.jsonl'
            file.write_text(''.join(json.dumps(r)+'\n' for r in [
                {'event':'recovery_feedback','operation':'0002_run','stamps':[10,1700000010],'monotonic':10},
                {'event':'recovery_feedback','operation':'0002_run','stamps':[11,1700000009],'monotonic':11}]))
            c=bridge.Collector(Path(d)/'observer',legacy_root=root);c.poll();rows=c.read(0,200)['events']
            self.assertEqual(rows[-1]['observed_at']-rows[0]['observed_at'],1)
            file.write_text('{"event":"recovery_feedback","stamps":[1,1800000001],"monotonic":1}\n');c.poll()
            self.assertEqual(c.state['anchors']['client_a'],1800000000)


class ContinuousSessionTests(unittest.TestCase):
    def session(self, root, name, stamp, rows=()):
        import os
        session = root/name
        session.mkdir(parents=True, exist_ok=True)
        path = session/'events.jsonl'
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        os.utime(path, (stamp, stamp))
        return session

    def rows(self, collector):
        return [json.loads(line) for line in collector.path.read_text().splitlines()]

    def archive(self, path):
        import numpy as np
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, state=np.arange(23), head_rgb=np.zeros((2,2,3), dtype=np.uint8),
                 left_wrist_rgb=np.zeros((2,2,3), dtype=np.uint8), right_wrist_rgb=np.zeros((2,2,3), dtype=np.uint8))

    def test_three_sessions_keep_active_identity_across_empty_log_late_writes_and_restart(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);root=p/'runtime'
            ignored=self.session(root, 'client_history', 1, [{'event':'target'}])
            ignored_empty=root/'client_empty_history';ignored_empty.mkdir()
            first=self.session(root, 'client_first', 2, [{'event':'recovery_feedback','operation':'0001_run'}])
            c=bridge.Collector(p/'observer', legacy_root=root);c.poll()
            self.assertEqual(c.health().get('active_session_id'), 'client_first')
            second=self.session(root, 'client_second', 3)
            c.poll()
            self.assertEqual(c.health().get('active_session_id'), 'client_second')
            (ignored_empty/'events.jsonl').write_text('{"event":"target"}\n')
            os.utime(ignored/'events.jsonl', (50,50))
            with (first/'events.jsonl').open('a') as f:f.write('{"event":"target","step":1}\n')
            c.poll()
            self.assertEqual(c.health().get('active_session_id'), 'client_second')
            third=self.session(root, 'client_third', 4, [{'event':'recovery_feedback','operation':'0001_run'}])
            c.poll()
            os.utime(second/'events.jsonl', (100,100))
            c=bridge.Collector(p/'observer', legacy_root=root)
            for _ in range(4):c.poll()
            self.assertEqual(c.health().get('active_session_id'), 'client_third')
            self.assertEqual({row['session_id'] for row in self.rows(c)}, {'client_first','client_third'})
            self.assertFalse(any(row['kind'] in ('request_started','operation_started') for row in self.rows(c)))

    def test_retired_session_tail_late_summary_and_unannounced_npz_are_drained_read_only(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);root=p/'runtime'
            old=self.session(root,'client_old',1,[{'event':'recovery_feedback','operation':'0001_run'}])
            c=bridge.Collector(p/'observer', legacy_root=root);c.poll()
            with (old/'events.jsonl').open('a') as f:
                for i in range(650):f.write(json.dumps({'event':'target','step':i})+'\n')
            os.utime(old/'events.jsonl',(2,2))
            current=self.session(root,'client_current',3,[{'event':'recovery_feedback','operation':'0001_run'}])
            self.archive(old/'0001_run/request_0007_obs.npz')
            summary=old/'0001_run/SUMMARY.json';summary.write_text(json.dumps({'mode':'run','result':{'finished':True}}))
            before={str(f):(f.read_bytes(),f.stat().st_mtime_ns,f.stat().st_ino) for f in root.rglob('*') if f.is_file()}
            for _ in range(12):
                count=len(self.rows(c))
                with patch.object(c,'_source',wraps=c._source) as reads:c.poll()
                self.assertLessEqual(reads.call_count,2)
                self.assertLessEqual(len(self.rows(c))-count,510)
            rows=self.rows(c)
            self.assertEqual(len([r for r in rows if r['session_id']=='client_old' and r['kind']=='target']),650)
            self.assertTrue(any(r['session_id']=='client_old' and r['kind']=='operation_finished' for r in rows))
            self.assertTrue(any(r['session_id']=='client_old' and r['kind']=='observation' and r['payload']['attempt']==7 for r in rows))
            self.assertEqual(c.health().get('active_session_id'),'client_current')
            after={str(f):(f.read_bytes(),f.stat().st_mtime_ns,f.stat().st_ino) for f in root.rglob('*') if f.is_file()}
            self.assertEqual(before,after)

    def test_same_session_late_operation_archive_and_rewritten_summary_keep_generation(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);root=p/'runtime'
            session=self.session(root,'client_a',1,[{'event':'recovery_feedback','operation':'0001_run'}])
            c=bridge.Collector(p/'observer', legacy_root=root);c.poll()
            with (session/'events.jsonl').open('a') as f:
                f.write('{"event":"recovery_feedback","operation":"0002_home"}\n')
                f.write('{"event":"recovery_feedback","operation":"0003_run"}\n')
            self.archive(session/'0001_run/request_0000_obs.npz')
            self.archive(session/'0003_run/request_0000_obs.npz')
            summary=session/'0003_run/SUMMARY.json';summary.write_text(json.dumps({'mode':'run','result':{'finished':True}}))
            for _ in range(8):c.poll()
            rows=self.rows(c)
            self.assertEqual({r['operation_id'] for r in rows if r['kind']=='observation'},{'0001_run','0003_run'})
            summary.write_text(json.dumps({'mode':'run','result':{'finished':True,'reason':'checked'}}))
            for _ in range(4):c.poll()
            self.assertTrue(any(r['kind']=='operation_finished' and r['payload']['result'].get('reason')=='checked' for r in self.rows(c)))
            (session/'events.jsonl').write_text('{"event":"recovery_feedback","operation":"0003_run","feedback23":[1]}\n')
            summary.write_text(json.dumps({'mode':'run','result':{'finished':False,'stopped':True}}))
            for _ in range(6):c.poll()
            finished=[r for r in self.rows(c) if r['kind']=='operation_finished']
            self.assertTrue(any(r['run_id']=='legacy:client_a:1:0003_run' and r['payload']['result'].get('stopped') for r in finished))
            self.assertEqual(len({r['event_id'] for r in finished}),3)

    def test_quiet_retired_sessions_stop_source_scans_and_restart_does_not_reactivate_history(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);root=p/'runtime'
            self.session(root,'client_first',1,[{'event':'recovery_feedback','operation':'0001_run'}])
            with patch.object(bridge.time,'time',return_value=1000):
                c=bridge.Collector(p/'observer', legacy_root=root);c.poll()
                self.session(root,'client_second',2,[{'event':'recovery_feedback','operation':'0001_run'}]);c.poll()
                self.session(root,'client_third',3,[{'event':'recovery_feedback','operation':'0001_run'}]);c.poll()
                for _ in range(8):c.poll()
            with patch.object(bridge.time,'time',return_value=1200):
                for _ in range(8):c.poll()
                c=bridge.Collector(p/'observer', legacy_root=root)
                with patch.object(c,'_source',wraps=c._source) as reads:
                    for _ in range(4):c.poll()
                self.assertEqual({call.args[0].parent.name for call in reads.call_args_list},{'client_third'})
                self.assertEqual(c.health().get('active_session_id'),'client_third')

    def test_retirement_records_missing_summary_and_incomplete_input_without_claiming_finish(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);root=p/'runtime'
            old=self.session(root,'client_old',1,[{'event':'recovery_feedback','operation':'0001_run'}])
            (old/'0001_run').mkdir();(old/'0001_run/request_0000_obs.npz').write_bytes(b'partial')
            with patch.object(bridge.time,'time',return_value=1000):
                c=bridge.Collector(p/'observer', legacy_root=root);c.poll()
                self.session(root,'client_new',2);c.poll()
            with patch.object(bridge.time,'time',return_value=1035):c.poll()
            with patch.object(bridge.time,'time',return_value=1200):
                for _ in range(4):c.poll()
            rows=[r for r in self.rows(c) if r['session_id']=='client_old']
            gaps=[r for r in rows if r['kind']=='capture_gap']
            self.assertTrue(any('summary' in r['payload']['reason'] for r in gaps))
            self.assertTrue(any('archive_input_unreadable' in r['payload']['reason'] for r in gaps))
            self.assertFalse(any(r['kind']=='operation_finished' for r in rows))
            self.assertEqual(c.health().get('active_session_id'),'client_new')

    def test_generation_rewrite_during_directory_scan_does_not_keep_retired_source_open(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);root=p/'runtime'
            old=self.session(root,'client_old',1,[{'event':'recovery_feedback','operation':'0001_run'}])
            for i in range(70):(old/('unused_%03d'%i)).mkdir()
            with patch.object(bridge.time,'time',return_value=1000):
                c=bridge.Collector(p/'observer',legacy_root=root);c.poll()
                (old/'events.jsonl').write_text('{"event":"recovery_feedback","operation":"0002_run"}\n')
                c.poll();self.session(root,'client_new',2)
                for _ in range(80):c.poll()
            with patch.object(bridge.time,'time',return_value=1200):
                for _ in range(8):c.poll()
                with patch.object(c,'_source',wraps=c._source) as reads:
                    for _ in range(3):c.poll()
                self.assertEqual({call.args[0].parent.name for call in reads.call_args_list},{'client_new'})

    def test_timed_out_npz_completed_in_place_is_retried_within_retired_window_after_restart(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);root=p/'runtime'
            old=self.session(root,'client_old',1,[{'event':'recovery_feedback','operation':'0001_run'}])
            archive=old/'0001_run';archive.mkdir();file=archive/'request_0000_obs.npz';file.write_bytes(b'partial')
            with patch.object(bridge.time,'time',return_value=1000):
                c=bridge.Collector(p/'observer',legacy_root=root);c.poll()
                self.session(root,'client_new',2);c.poll()
            with patch.object(bridge.time,'time',return_value=1035):c.poll()
            directory_mtime=archive.stat().st_mtime_ns
            self.archive(file)
            self.assertEqual(archive.stat().st_mtime_ns,directory_mtime)
            with patch.object(bridge.time,'time',return_value=1040):
                c=bridge.Collector(p/'observer',legacy_root=root)
                for _ in range(4):c.poll()
            rows=[r for r in self.rows(c) if r['session_id']=='client_old']
            self.assertEqual(len([r for r in rows if r['kind']=='capture_gap']),1)
            self.assertEqual(len([r for r in rows if r['kind']=='observation']),1)
            self.assertTrue(any(r['kind']=='artifact_ready' and r['payload']['status']=='ready' for r in rows))
            self.assertEqual(c.health().get('active_session_id'),'client_new')

    def test_native_only_collector_health_does_not_hide_its_live_run(self):
        from exhibit.vla_evidence import VLAStore
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);source=p/'native.jsonl'
            source.write_text(json.dumps(dict(schema_version=1,event_id='native:1',robot_id='robot239',session_id='native_session',
                run_id='native_run',operation_id='native_op',kind='operation_started',observed_at=100,
                payload={'mode':'run'}))+'\n')
            c=bridge.Collector(p/'observer',telemetry_file=source);c.poll();batch=c.read(0,200)
            store=VLAStore(':memory:')
            try:
                store.ingest(batch['events'],batch['source_id'],batch['next_cursor'],batch['health'])
                self.assertEqual(store.live(now=101)['state']['run_id'],'native_run')
            finally:store.close()
