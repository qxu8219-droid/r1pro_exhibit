import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vla_evidence import VLAStore


def event(kind, seq, payload=None, request_id=None, run_id='run-1', operation_id='op-1'):
    value = dict(schema_version=1, event_id='producer:%s' % seq, robot_id='r1', session_id='s1',
                 run_id=run_id, operation_id=operation_id, kind=kind, observed_at=100 + seq,
                 payload=payload or {})
    if request_id is not None:
        value['request_id'] = request_id
    return value


class VLAEvidenceTests(unittest.TestCase):
    def test_precheck_failure_is_visible_without_inventing_a_vla_request(self):
        self.store.ingest([event('operation_finished', 1, {'mode': 'precheck',
            'result_kind': 'CLIENT_ERROR', 'result': {'reason': 'ROS graph not ready', 'stopped': False}})],
            'source', 10, {'observed_at': 101, 'legacy': True})
        state = self.store.live(now=102)['state']
        self.assertEqual(state['phase'], 'precheck_failed')
        self.assertEqual(state['reason'], 'ROS graph not ready')
        self.assertIsNone(state['request']['id'])
        self.assertIsNone(state['verification']['run_finished'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'vla.sqlite3'
        self.store = VLAStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ingest(self, *events):
        self.store.ingest(list(events), 'source', 1000, {'observed_at': 200, 'dropped': 2, 'legacy': False})
        return self.store.live(now=201)['state']

    def test_retry_clears_response_and_images_and_late_attempt_cannot_replace(self):
        state = self.ingest(event('operation_started', 1, {'mode': 'run', 'task': 'pick'}),
            event('request_started', 2, {'attempt': 1, 'state23': [1] * 23}, 'op-1:1'),
            event('raw_response', 3, {'attempt': 1, 'chunk': 1, 'response': {'action23': [2] * 23}}, 'op-1:1'),
            event('artifact_ready', 4, {'attempt': 1, 'images': {'head_rgb': 'missing'}}, 'op-1:1'),
            event('request_failed', 5, {'attempt': 1}, 'op-1:1'),
            event('request_started', 6, {'attempt': 2, 'state23': [3] * 23}, 'op-1:2'),
            event('raw_response', 7, {'attempt': 1, 'response': {'action23': [9] * 23}}, 'op-1:1'),
            event('artifact_ready', 8, {'attempt': 1, 'images': {'head_rgb': 'pending'}}, 'op-1:1'))
        self.assertEqual(state['request']['state23'], [3] * 23)
        self.assertEqual(state['request']['status'], 'started')
        self.assertIsNone(state['request']['action23'])
        self.assertIsNone(state['request']['images'])
        self.assertEqual(state['stats']['failed_requests'], 1)

    def test_target_feedback_is_explicitly_prepublish_until_independent_sample(self):
        state = self.ingest(event('target', 1, {'chunk': 1, 'step': 0, 'target23': [1] * 23, 'feedback23': [2] * 23}))
        self.assertEqual(state['execution']['feedback_kind'], 'prepublish')
        state = self.ingest(event('recovery_feedback', 2, {'feedback23': [3] * 23, 'sample_time': 101.5}))
        self.assertEqual(state['execution']['feedback_kind'], 'recovery_feedback')
        self.assertEqual(state['execution']['feedback_at'], 102)
        self.assertEqual(state['execution']['issued_steps'], 1)

    def test_finish_and_home_do_not_claim_task_success_and_run_switch_clears(self):
        state = self.ingest(event('operation_started', 1, {'mode': 'run', 'task': 'pick'}),
            event('operation_finished', 2, {'mode': 'run', 'result': {'finished': True, 'success': True}}),
            event('operation_finished', 3, {'mode': 'home', 'result': {'success': True}}, operation_id='home-1'))
        self.assertEqual(state['verification']['status'], 'pending')
        self.assertTrue(state['verification']['run_finished'])
        self.assertTrue(state['verification']['home_success'])
        state = self.ingest(event('operation_started', 4, {'mode': 'run', 'task': 'new'}, run_id='run-2', operation_id='op-2'))
        self.assertIsNone(state['verification']['run_finished'])
        self.assertIsNone(state['request']['action23'])
        self.assertEqual(len(self.store.runs()), 2)

    def test_cursor_dedup_and_restart_are_persistent(self):
        e = event('status', 1, {'state': 'ready'})
        self.store.ingest([e], 'source', 20, {'observed_at': 200})
        self.store.ingest([e], 'source', 20, {'observed_at': 201})
        self.assertEqual(self.store.cursor('source'), 20)
        self.assertEqual(self.store.cursor('unknown'), 0)
        self.assertEqual(len(self.store.events('run-1')['events']), 1)
        self.store.close()
        self.store = VLAStore(self.path)
        self.assertEqual(self.store.cursor('source'), 20)
        self.assertEqual(self.store.live(now=202)['state']['stats']['events'], 1)
        self.store.ingest([], 'source', 10)
        self.assertEqual(self.store.cursor('source'), 20)

    def test_invalid_numbers_rejected_atomically_with_cursor(self):
        for invalid in (float('nan'), float('inf'), True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.store.ingest([event('observation', 1, {'state23': [invalid] * 23})], 'source', 30)
                self.assertEqual(self.store.cursor('source'), 0)
        self.assertEqual(self.store.events('run-1')['events'], [])

    def test_transport_age_stale_preserves_evidence(self):
        self.ingest(event('request_started', 1, {'attempt': 1}, 'op-1:1'))
        result = self.store.live(now=240)
        self.assertEqual(result['status'], 'stale')
        self.assertFalse(result['connected'])
        self.assertEqual(result['state']['phase'], 'stale')
        self.assertEqual(result['state']['request']['id'], 'op-1:1')

    def test_sanitizes_payload_and_bounds_free_text(self):
        state = self.ingest(event('operation_started', 1, {'mode': 'run', 'task': 'pick',
            'config': {'password': 'secret'}, 'archive': '/home/operator/private.npz'}),
            event('raw_response', 2, {'response': {'cot_text': 'a' * 20000, 'api_key': 'secret'},
                'config': {'secret': 'secret'}}, 'op-1:1'))
        raw = json.dumps(self.store.events('run-1'))
        self.assertNotIn('secret', raw)
        self.assertNotIn('/home/operator', raw)
        self.assertLess(len(raw), 20000)
        self.assertEqual(state['verification']['status'], 'pending')

    def test_new_idle_source_does_not_resurrect_old_active_run(self):
        self.store.ingest([event('operation_started', 1, {'mode': 'run'})], 'old-source', 10, {'observed_at': 101})
        self.store.ingest([], 'new-source', 0, {'observed_at': 200})
        self.assertIsNone(self.store.live(now=201)['state']['run_id'])
        self.assertEqual(len(self.store.runs()), 1)

    def test_heartbeat_keeps_executor_fresh_without_replacing_phase(self):
        self.store.ingest([event('request_started', 1, {'attempt': 1}, 'op-1:1')], 'source', 10, {'observed_at': 200})
        self.assertEqual(self.store.live(now=200)['status'], 'stale')
        heartbeat = event('heartbeat', 2)
        heartbeat['observed_at'] = 200
        self.store.ingest([heartbeat], 'source', 20, {'observed_at': 200})
        state = self.store.live(now=201)
        self.assertTrue(state['connected'])
        self.assertEqual(state['state']['phase'], 'request_started')

    def test_late_old_observation_cannot_replace_new_attempt(self):
        state = self.ingest(event('request_started', 1, {'attempt': 2, 'state23': [2] * 23}, 'op-1:2'),
            event('observation', 2, {'attempt': 1, 'state23': [1] * 23}, 'op-1:1'))
        self.assertEqual(state['request']['id'], 'op-1:2')
        self.assertEqual(state['request']['status'], 'started')

    def test_cross_connection_reads_and_absolute_path(self):
        other = VLAStore(self.path)
        try:
            self.store.ingest([event('status', 1, {'state': 'ready'})], 'source', 10)
            self.assertEqual(other.live(now=101)['state']['phase'], 'ready')
            self.assertEqual(other.cursor('source'), 10)
            self.assertTrue(Path(other.path).is_absolute())
        finally:
            other.close()

    def test_new_operation_attempt_number_can_restart(self):
        state = self.ingest(event('operation_started', 1, {'mode': 'run'}),
            event('request_started', 2, {'attempt': 5}, 'op-1:5'),
            event('operation_started', 3, {'mode': 'home'}, operation_id='op-home'),
            event('request_started', 4, {'attempt': 1}, 'op-home:1', operation_id='op-home'))
        self.assertEqual(state['request']['id'], 'op-home:1')

    def test_response_restores_matching_actual_request_state(self):
        state = self.ingest(event('request_started', 1, {'attempt': 1}, 'op-1:1'),
            event('raw_response', 2, {'attempt': 1, 'request_state23': [8] * 23,
                'capture_at': 98, 'response': {'action23': [9] * 23}}, 'op-1:1'))
        self.assertEqual(state['request']['state23'], [8] * 23)
        self.assertEqual(state['request']['capture_at'], 98)

    def test_legacy_response_projects_actual_matrix_without_invented_start(self):
        state = self.ingest(event('raw_response', 1, {'attempt': 3, 'request_state23': [2] * 23,
            'response': {'action23': [[4] * 23] * 32}}, 'op-1:3'))
        self.assertEqual(state['request']['action23'], [[4] * 23] * 32)
        self.assertEqual(state['request']['status'], 'responded')
        self.assertIsNone(state['request']['started_at'])
        self.assertEqual(state['request']['state23'], [2] * 23)
        state = self.ingest(event('raw_response', 2, {'attempt': 2,
            'response': {'action23': [[8] * 23]}}, 'op-1:2'))
        self.assertEqual(state['request']['id'], 'op-1:3')

    def test_actual_timing_fields_and_monotonic_feedback_are_preserved(self):
        response = event('raw_response', 1, {'attempt': 1,
            'request_timing': {'request': 7, 'infer_started': 23, 'wire': {'encode_started': 22}},
            'observation_meta': {'timing': {'source_skew_s': 0.2, 'receipt_age_s': {'head_rgb': 0.1}}},
            'response': {'action23': [[1] * 23],
                'inference_timing_ms': {'infer_ms': 20, 'fm_action_ms': 11},
                'cot_generation': {'generated_tokens_including_stop': [21], 'stop_token_ids': [252187], 'stop_reason': 'stop_token'}}}, 'op-1:1')
        feedback = event('recovery_feedback', 2, {'feedback23': [2] * 23, 'sample_time': 17.5})
        feedback['monotonic'] = 18
        state = self.ingest(response, feedback)
        self.assertEqual(state['request']['inference_timing_ms']['infer_ms'], 20)
        self.assertEqual(state['execution']['feedback_at'], 101.5)
        saved = self.store.events('run-1')['events'][0]['payload']
        self.assertEqual(saved['request_timing']['wire']['encode_started'], 22)
        self.assertEqual(saved['observation_meta']['timing']['receipt_age_s']['head_rgb'], 0.1)
        self.assertEqual(saved['response']['cot_generation']['stop_reason'], 'stop_token')

    def test_malformed_nested_payload_cannot_poison_live_reads(self):
        for kind, payload in [('raw_response', {'response': 'bad'}),
                              ('operation_finished', {'result': True}),
                              ('artifact_ready', {'images': []})]:
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    self.store.ingest([event(kind, 1, payload)], 'source', 10)
                self.assertEqual(self.store.cursor('source'), 0)

    def test_review_late_archive_cannot_select_run_or_refresh_executor(self):
        old = event('raw_response', 1, {'attempt': 1}, 'old:1', run_id='old')
        new = event('raw_response', 2, {'attempt': 1}, 'new:1', run_id='new')
        old['observed_at'], new['observed_at'] = 10, 100
        artifact = event('artifact_ready', 3, {'attempt': 1, 'images': {'head_rgb': 'pending'}}, 'old:1', run_id='old')
        artifact['observed_at'] = 1000
        diagnostic = event('diagnostic', 4, run_id='old')
        diagnostic['observed_at'] = 1001
        self.store.ingest([new, old, artifact, diagnostic], 'source', 10, {'observed_at': 1001})
        live = self.store.live(now=120)
        self.assertEqual(live['state']['run_id'], 'new')
        self.assertEqual(live['status'], 'stale')
        self.assertEqual(live['age_s'], 20)
        self.assertEqual(live['freshness_s'], 15)

    def test_review_same_request_start_and_late_data_preserve_terminal_states(self):
        state = self.ingest(event('raw_response', 1, {'attempt': 1, 'response': {'action23': [[1] * 23]}}, 'op-1:1'),
            event('request_started', 2, {'attempt': 1}, 'op-1:1'),
            event('operation_finished', 3, {'mode': 'run', 'result': {'finished': True}}),
            event('raw_response', 4, {'attempt': 1, 'response': {'action23': [[2] * 23]}}, 'op-1:1'),
            event('target', 5, {'target23': [2] * 23}))
        self.assertEqual(state['request']['status'], 'responded')
        self.assertEqual(self.store.live(now=105)['state']['phase'], 'run_finished')
        self.assertTrue(state['verification']['run_finished'])

    def test_review_standalone_failure_or_cancel_cannot_be_resurrected(self):
        for kind, status in [('request_failed', 'failed'), ('request_cancelled', 'cancelled')]:
            with self.subTest(kind=kind):
                run = 'run-' + kind
                seq = 1 if kind == 'request_failed' else 4
                state = self.ingest(event(kind, seq, {'attempt': 1}, 'op-1:1', run_id=run),
                    event('raw_response', seq + 1, {'attempt': 1, 'response': {'action23': [[1] * 23]}}, 'op-1:1', run_id=run),
                    event('request_started', seq + 2, {'attempt': 1}, 'op-1:1', run_id=run))
                self.assertEqual(state['request']['id'], 'op-1:1')
                self.assertEqual(state['request']['status'], status)
                self.assertIsNone(state['request']['action23'])

    def test_review_empty_health_source_is_waiting_and_clears_old_run(self):
        self.store.ingest([event('request_started', 1, {'attempt': 1}, 'op-1:1')], 'old', 10)
        self.store.ingest([], 'new', 0, {'observed_at': None, 'dropped': 0, 'legacy': False})
        live = self.store.live(now=200)
        self.assertFalse(live['connected'])
        self.assertEqual(live['status'], 'waiting')
        self.assertIsNone(live['state']['run_id'])

    def test_review_arithmetic_and_result_types_reject_before_cursor_commit(self):
        cases = [('recovery_feedback', {'sample_time': 'bad'}),
                 ('raw_response', {'rtt_s': 'bad'}),
                 ('request_started', {'capture_at': {}}),
                 ('operation_finished', {'result': {'finished': 'true'}}),
                 ('operation_finished', {'result': {'success': 1}})]
        for kind, payload in cases:
            with self.subTest(kind=kind, payload=payload):
                bad = event(kind, 1, payload)
                bad['monotonic'] = 1
                with self.assertRaises(ValueError):
                    self.store.ingest([bad], 'source', 99)
                self.assertEqual(self.store.cursor('source'), 0)
        self.assertEqual(self.store.events('run-1')['events'], [])

    def test_review_source_timestamps_order_backfill_and_ignore_future_clock(self):
        earlier = event('raw_response', 1, {'attempt': 1}, 'old:1', run_id='old')
        later = event('request_started', 2, {'attempt': 1}, 'new:1', run_id='new')
        earlier['observed_at'], later['observed_at'] = 10, 100
        future = event('heartbeat', 3, run_id='old')
        future['observed_at'] = 10000
        self.store.ingest([later, earlier, future], 'source', 10, {'observed_at': 100})
        live = self.store.live(now=101)
        self.assertEqual(live['state']['run_id'], 'new')
        self.assertTrue(live['connected'])

    def test_unrelated_status_does_not_replace_active_run(self):
        state = self.ingest(event('operation_started', 1, {'mode': 'run', 'task': 'pick'}),
            event('status', 2, {'state': 'ready', 'legacy': True}, run_id='legacy-unrelated'))
        self.assertEqual(state['run_id'], 'run-1')
        self.assertEqual(state['task'], 'pick')


if __name__ == '__main__':
    unittest.main()
