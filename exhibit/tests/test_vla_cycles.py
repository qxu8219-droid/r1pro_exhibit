"""Repeated-run integration through the actual local collector HTTP protocol."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'exhibit'))
from robot_side.bridge import Collector, make_server
from vla_evidence import VLAStore
from vla_receiver import Receiver


class CycleIntegrationTests(unittest.TestCase):
    def test_original_process_cycles_and_new_process_start_do_not_mix_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'original-runtime'
            old = source / 'client_old'
            old_op = old / '0002_run'
            old_op.mkdir(parents=True)
            epoch = time.time() - 5
            def rows(value, mono=1):
                return [dict(event='recovery_feedback', operation='0002_run', monotonic=mono,
                             sample_time=mono, stamps=[mono, epoch + mono], feedback23=[value] * 23),
                        dict(event='raw_response', chunk=0, monotonic=mono + .1,
                             request_state23=[value] * 23, request_timing={'request': 0},
                             response={'action23': [[value] * 23] * 32})]
            old_log = old / 'events.jsonl'
            old_log.write_text(''.join(json.dumps(e) + '\n' for e in rows(1)))
            collector = Collector(root / 'observer', legacy_root=source, robot_id='r1')
            store = VLAStore(root / 'vla.sqlite3')
            server = make_server('127.0.0.1', 0, collector)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            receiver = Receiver('http://127.0.0.1:%d' % server.server_port, store)
            def tick():
                collector.poll()
                receiver.poll_once()
                return store.live(now=time.time() + 10)
            try:
                first = tick()['state']
                self.assertEqual(first['request']['action23'], [[1] * 23] * 32)
                self.assertEqual(first['session_id'], 'client_old')
                # The old process can finish writing after a new session exists.
                new = source / 'client_new'
                new.mkdir()
                new_log = new / 'events.jsonl'
                new_log.write_text('')
                with old_log.open('a') as f:
                    f.write(json.dumps(dict(event='target', monotonic=2, chunk=0, step=0,
                                           target23=[3] * 23, feedback23=[1] * 23)) + '\n')
                (old_op / 'SUMMARY.json').write_text(json.dumps(dict(mode='run', event_kind='RUN_FINISHED',
                                                                    result={'finished': True, 'stopped': True})))
                os.utime(old_log, (time.time() + 5, time.time() + 5))
                expected = old_log.read_bytes()
                empty = tick()
                self.assertEqual(empty['status'], 'waiting')
                self.assertEqual(empty['state']['session_id'], 'client_new')
                self.assertIsNone(empty['state']['request']['action23'])
                for _ in range(4): tick()
                archived = next(r for r in store.runs() if r['session_id'] == 'client_old')
                self.assertEqual(archived['execution']['issued_steps'], 1)
                self.assertTrue(archived['verification']['run_finished'])
                # Same request number in a different process is a different request context.
                (new / '0002_run').mkdir()
                new_log.write_text(''.join(json.dumps(e) + '\n' for e in rows(8, 3)))
                second = tick()['state']
                self.assertEqual(second['session_id'], 'client_new')
                self.assertEqual(second['request']['action23'], [[8] * 23] * 32)
                self.assertEqual(second['execution']['issued_steps'], 0)
                self.assertIsNone(second['verification']['run_finished'])
                self.assertEqual(old_log.read_bytes(), expected)
            finally:
                server.shutdown(); server.server_close(); worker.join(2)
                store.close()


if __name__ == '__main__':
    unittest.main()
