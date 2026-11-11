"""Kiểm thử ngoại tuyến nhật ký HTTP arXiv và giới hạn khi tiếp tục phiên."""
import copy
from datetime import datetime, timezone
from email.utils import formatdate
import json
import unittest
from unittest.mock import patch

import test_arxiv_client as cf
import test_pipeline as fx
from src import arxiv_client as client, batch, benchmark_runner
from src.storage import CorpusStorage
from src.pipeline import PipelineStopped

class TransportAuditTests(unittest.TestCase):
    setUp = cf.ArxivClientTests.setUp
    open_response = cf.ArxivClientTests.open_response
    query = cf.ArxivClientTests.query

    def observe(self, **options):
        events = []
        with client.record_requests(events.append):
            result = self.query(**options)
        return result, events

    def test_success_timestamp_endpoint_status_and_identity(self):
        self.outcomes = [cf.FakeResponse()]
        _, events = self.observe()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event['request_started_at_utc'], client._utc(self.clock.wall_time()))
        self.assertEqual(event['http_status'], 200)
        self.assertEqual(event['endpoint'], client.API_URL)
        self.assertEqual(event['query_identifier'], cf.ARXIV_ID)
        self.assertEqual(event['attempt'], 1)
        self.assertIsNone(event['retry_after'])
        self.assertIsNone(event['cooldown_deadline_utc'])

    def test_numeric_retry_after_uses_response_receipt_not_request_start(self):
        self.outcomes = [cf.http_error(429, '120')]
        original = self.opener.open.side_effect
        def delayed(*args, **kwargs):
            self.clock.now += 2
            return original(*args, **kwargs)
        self.opener.open.side_effect = delayed
        result, events = self.observe(max_attempts=1)
        event = events[0]
        started = client._parse_utc(event['request_started_at_utc']).timestamp()
        received = client._parse_utc(event['response_received_at_utc']).timestamp()
        self.assertEqual(received - started, 2)
        self.assertEqual(client._parse_utc(event['cooldown_deadline_utc']).timestamp() - received, 120)
        self.assertEqual(event['retry_after'], '120')
        self.assertEqual((result['status'], result['attempts']), ('rate_limited', 1))
        self.sleep.assert_not_called()

    def test_http_date_deadline_is_preserved(self):
        raw = formatdate(self.clock.wall_time() + 90, usegmt=True)
        self.outcomes = [cf.http_error(429, raw)]
        _, events = self.observe(max_attempts=1)
        self.assertEqual(events[0]['retry_after'], raw)
        self.assertEqual(events[0]['cooldown_deadline_utc'], client._utc(self.clock.wall_time() + 90))

    def test_missing_invalid_past_and_overflow_headers_do_not_invent_deadlines(self):
        for raw in (None, 'invalid', '-1', '1.5', '9' * 500):
            with self.subTest(raw=raw):
                self.outcomes = [cf.http_error(429, raw)]
                _, events = self.observe(max_attempts=1)
                self.assertEqual(events[0]['retry_after'], raw)
                self.assertIsNone(events[0]['cooldown_deadline_utc'])
        past = formatdate(self.clock.wall_time() - 10, usegmt=True)
        self.assertLess(client._parse_utc(client._retry_deadline(past, client._utc(self.clock.wall_time()))).timestamp(),
                        self.clock.wall_time())

    def test_retry_attempts_are_logged_once_and_pacing_unchanged(self):
        self.outcomes = [cf.http_error(429, '12'), cf.FakeResponse()]
        result, events = self.observe()
        self.assertEqual([e['http_status'] for e in events], [429, 200])
        self.assertEqual([e['attempt'] for e in events], [1, 2])
        self.assertEqual(self.starts, [0, 12])
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(client.MIN_INTERVAL, 3.0)

    def test_timeout_keeps_unknown_http_status(self):
        self.outcomes = [TimeoutError('offline')]
        _, events = self.observe(max_attempts=1)
        self.assertIsNone(events[0]['http_status'])
        self.assertIsNone(events[0]['response_received_at_utc'])
        self.assertIsNone(events[0]['cooldown_deadline_utc'])

    def test_audit_omits_secrets_arbitrary_query_headers_and_error_messages(self):
        marker = 'fixture-secret-do-not-log'
        error = cf.http_error(403)
        error.msg = marker
        error.headers['x-api-key'] = marker
        self.outcomes = [error]
        events = []
        with client.record_requests(events.append):
            client._request_arxiv(client.API_URL + '?id_list=' + cf.ARXIV_ID + '&token=' + marker,
                timeout=15, max_attempts=1, user_agent=marker,
                accept='application/atom+xml', read_response=lambda response: {})
        self.assertNotIn(marker, json.dumps(events))
        self.assertEqual(events[0]['http_status'], 403)


class BatchAuditTests(unittest.TestCase):
    setUp = fx.PipelineTests.setUp
    sleep = fx.PipelineTests.sleep
    open_response = fx.PipelineTests.open_response

    def run_batch(self, **settings):
        options = dict(batch_id='cooldown', max_ids=1, log_root=self.root.parent/'logs',
                       timeout=15, max_attempts=1, revision_history=True)
        options.update(settings)
        return batch.run_batch([fx.PAIR], self.root, **options)

    def seed_stop(self, raw='120'):
        self.outcomes = [fx.xml_response(), fx.http_error(429, raw)]
        return self.run_batch()

    def snapshot(self):
        return {p.relative_to(self.root.parent).as_posix(): p.read_bytes()
                for p in self.root.parent.rglob('*') if p.is_file()}

    def log_path(self):
        return next((self.root.parent/'logs/cooldown/attempts').glob('*.json'))

    def test_attempt_persists_metadata_and_source_events_before_stop(self):
        value = self.seed_stop()
        self.assertEqual(value, json.loads(self.log_path().read_text(encoding='utf-8')))
        events = value['arxiv_http_requests']
        self.assertEqual([e['http_status'] for e in events], [200, 429])
        self.assertEqual(events[1]['endpoint'], 'https://export.arxiv.org/src/'+fx.PAIR[1]+'v1')
        self.assertEqual(events[1]['query_identifier'], fx.PAIR[1]+'v1')
        self.assertEqual(events[1]['retry_after'], '120')
        self.assertEqual(value['http_metrics']['total']['attempts'], 2)
        self.assertEqual(value['http_metrics']['total']['retry_attempts'], 0)
        self.assertEqual(value['status'], 'stopped')
        self.assertEqual(CorpusStorage(self.root).read_manifest(*fx.PAIR)['versions']['v1']['status'], 'rate_limited')

    def test_restart_numeric_cooldown_blocks_before_requests_or_writes(self):
        value = self.seed_stop()
        before, count = self.snapshot(), len(self.requests)
        with patch.object(client, '_next_request_at', 0.0):
            with self.assertRaises(client.ArxivCooldownError) as caught:
                self.run_batch(resume=True)
        self.assertEqual(caught.exception.retry_at_utc, value['arxiv_http_requests'][-1]['cooldown_deadline_utc'])
        self.assertEqual(len(self.requests), count)
        self.assertEqual(before, self.snapshot())

    def test_restart_http_date_and_provider_cooldown_blocks_other_batch(self):
        raw = formatdate(1700000000 + 90, usegmt=True)
        self.seed_stop(raw)
        before = self.snapshot()
        with patch.object(client, '_next_request_at', 0.0):
            with self.assertRaises(client.ArxivCooldownError):
                self.run_batch(batch_id='other-cohort')
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.requests), 2)

    def test_expired_deadline_allows_one_source_request_without_discovery(self):
        self.seed_stop()
        path = self.log_path()
        old = path.read_bytes()
        self.now += 121
        self.outcomes = [fx.Response(fx.TEX)]
        with patch.object(client, '_next_request_at', 0.0):
            value = self.run_batch(resume=True)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(value['papers'][0]['result']['discovery']['action'], 'cached')
        self.assertEqual(len(value['arxiv_http_requests']), 1)
        self.assertEqual(old, path.read_bytes())
        self.assertTrue(CorpusStorage(self.root).should_skip_version(*fx.PAIR, 'v1'))

    def test_missing_header_has_no_fabricated_cooldown(self):
        self.seed_stop(None)
        batch.check_arxiv_cooldown(self.root.parent/'logs')
        self.assertIsNone(json.loads(self.log_path().read_text())['arxiv_http_requests'][-1]['cooldown_deadline_utc'])

    def test_legacy_log_without_events_is_readable_and_not_rewritten(self):
        self.outcomes = [fx.xml_response(), fx.Response(fx.TEX)]
        self.run_batch()
        path = self.log_path()
        old = json.loads(path.read_text())
        del old['arxiv_http_requests']
        path.write_text(json.dumps(old))
        before = path.read_bytes()
        batch.check_arxiv_cooldown(self.root.parent/'logs')
        result = self.run_batch(resume=True)
        self.assertEqual(result['arxiv_http_requests'], [])
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(len(self.requests), 2)

    def test_invalid_deadline_evidence_fails_closed_and_preserves_bytes(self):
        self.seed_stop()
        path = self.log_path()
        original = json.loads(path.read_text())
        for bad in ({}, [], 'not-a-date', None):
            value = copy.deepcopy(original)
            value['arxiv_http_requests'][-1]['cooldown_deadline_utc'] = bad
            path.write_text(json.dumps(value))
            before = self.snapshot()
            with self.subTest(bad=bad), self.assertRaises(batch.BatchError):
                self.run_batch(resume=True)
            self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.requests), 2)

    def test_http_event_write_failure_stops_without_following_request(self):
        self.outcomes = [fx.xml_response(), fx.Response(fx.TEX)]
        original = batch._save
        def fail_event(path, value):
            if value.get('arxiv_http_requests'):
                raise batch.BatchLogError('offline event write failure')
            return original(path, value)
        with patch.object(batch, '_save', side_effect=fail_event):
            with self.assertRaises(batch.BatchLogError):
                self.run_batch()
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(client._REQUEST_LOG.get())
        self.assertFalse(client._request_lock.locked())

    def test_deadline_is_durable_even_if_source_checkpoint_save_fails(self):
        self.outcomes = [fx.xml_response(), fx.http_error(429, '60')]
        with patch.object(CorpusStorage, 'record_source_result', side_effect=OSError('offline checkpoint')):
            with self.assertRaises(PipelineStopped):
                self.run_batch()
        before = self.snapshot()
        with patch.object(client, '_next_request_at', 0.0):
            with self.assertRaises(client.ArxivCooldownError):
                self.run_batch(resume=True)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.requests), 2)


class ExperimentalTransportRetryTests(unittest.TestCase):
    setUp = cf.ArxivClientTests.setUp
    open_response = cf.ArxivClientTests.open_response
    query = cf.ArxivClientTests.query

    def run_retry(self, budget=300, sink=None):
        events = []
        with client.retry_429_policy(budget) as policy, client.record_requests(
                events.append if sink is None else sink):
            result = self.query(max_attempts=1)
        self.assertIsNone(client._RETRY_429.get())
        return result, events, policy

    def test_429_then_200_same_request_and_logged_before_sleep(self):
        self.outcomes = [cf.http_error(429, '12'), cf.FakeResponse()]
        events = []
        def sleep(delay):
            self.assertEqual([e['http_status'] for e in events], [429])
            self.assertEqual(events[0]['retry_429_decision'], 'retry_once')
            self.clock.sleep(delay)
        self.sleep.side_effect = sleep
        result, _, policy = self.run_retry(sink=events.append)
        self.assertEqual((result['status'], result['attempts']), ('found', 2))
        self.assertEqual(self.starts, [0, 12])
        self.assertIs(self.opener.open.call_args_list[0].args[0],
                      self.opener.open.call_args_list[1].args[0])
        self.assertEqual([e['http_status'] for e in events], [429, 200])
        self.assertEqual(policy.waited, 12)
        self.assertEqual(client.MIN_INTERVAL, 3)

    def test_missing_and_invalid_retry_after_use_separate_60_second_deadline(self):
        for raw in (None, 'invalid', '-1', '1.5', '9' * 500):
            with self.subTest(raw=raw):
                self.clock.now = 0
                client._next_request_at = 0
                self.starts.clear()
                self.outcomes = [cf.http_error(429, raw), cf.FakeResponse()]
                result, events, policy = self.run_retry()
                event = events[0]
                self.assertEqual(result['http_status'], 200)
                self.assertIsNone(event['cooldown_deadline_utc'])
                self.assertEqual(event['retry_wait_source'], 'internal_backoff')
                self.assertEqual(event['retry_wait_deadline_utc'], client._utc(1700000060))
                self.assertEqual(self.starts, [0, 60])
                self.assertEqual(policy.waited, 60)
                self.assertEqual(policy.snapshot()['fallback_seconds'], 60)

    def test_http_date_preferred_and_minimum_pacing_for_valid_zero_header(self):
        for raw, wait in ((formatdate(1700000090, usegmt=True), 90), ('0', 3)):
            with self.subTest(raw=raw):
                self.clock.now = 0
                client._next_request_at = 0
                self.starts.clear()
                self.outcomes = [cf.http_error(429, raw), cf.FakeResponse()]
                _, events, _ = self.run_retry()
                self.assertEqual(events[0]['retry_wait_source'], 'retry_after')
                self.assertIsNotNone(events[0]['cooldown_deadline_utc'])
                self.assertEqual(events[0]['retry_wait_deadline_utc'], client._utc(1700000000+wait))
                self.assertEqual(self.starts, [0, wait])

    def test_repeated_429_stops_after_one_retry_and_keeps_next_deadline(self):
        self.outcomes = [cf.http_error(429), cf.http_error(429)]
        result, events, policy = self.run_retry()
        self.assertEqual((result['status'], result['attempts']), ('rate_limited', 2))
        self.assertEqual(self.starts, [0, 60])
        self.assertEqual(events[-1]['retry_429_decision'], 'retry_exhausted')
        self.assertEqual(events[-1]['retry_wait_deadline_utc'], client._utc(1700000120))
        self.assertEqual(policy.waited, 60)

    def test_long_wait_exceeds_budget_without_sleep_or_second_request(self):
        self.outcomes = [cf.http_error(429, '301')]
        result, events, policy = self.run_retry()
        self.assertEqual(result['http_status'], 429)
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(events[0]['retry_429_decision'], 'budget_exceeded')
        self.assertEqual(events[0]['cooldown_deadline_utc'], client._utc(1700000301))
        self.assertTrue(policy.exhausted)
        self.sleep.assert_not_called()
        self.assertEqual(self.opener.open.call_count, 1)

    def test_budget_is_shared_by_requests_not_reset_per_request(self):
        self.outcomes = [cf.http_error(429, '200'), cf.FakeResponse(),
                         cf.http_error(429)]
        events = []
        with client.retry_429_policy(250) as policy, client.record_requests(events.append):
            self.query(max_attempts=1)
            result = self.query(max_attempts=1)
        self.assertEqual(result['http_status'], 429)
        self.assertEqual(events[-1]['retry_429_decision'], 'budget_exceeded')
        self.assertEqual(policy.waited, 200)
        self.assertEqual(self.starts, [0, 200, 203])

    def test_403_and_network_failure_are_not_retried(self):
        for error in (cf.http_error(403), TimeoutError('offline timeout')):
            with self.subTest(error=type(error).__name__):
                client._next_request_at = 0
                self.outcomes = [error]
                count = self.opener.open.call_count
                result, events, policy = self.run_retry()
                self.assertEqual(self.opener.open.call_count, count + 1)
                self.assertEqual(result['attempts'], 1)
                self.assertNotIn('retry_wait_source', events[0])
                self.assertEqual(policy.waited, 0)

    def test_log_failure_prevents_sleep_and_retry(self):
        self.outcomes = [cf.http_error(429)]
        def fail(event):
            raise batch.BatchLogError('offline log write')
        with self.assertRaises(batch.BatchLogError):
            self.run_retry(sink=fail)
        self.sleep.assert_not_called()
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertIsNone(client._RETRY_429.get())

    def test_sleep_overshoot_does_not_send_request_over_budget(self):
        self.outcomes = [cf.http_error(429)]
        self.sleep.side_effect = lambda delay: self.clock.sleep(delay + 1)
        result, _, policy = self.run_retry(budget=60)
        self.assertEqual(result['http_status'], 429)
        self.assertTrue(policy.exhausted)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_invalid_budget_and_unlogged_or_unbounded_usage_rejected_before_http(self):
        for bad in (True, -1, '300', float('inf'), float('nan')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                with client.retry_429_policy(bad):
                    pass
        with client.retry_429_policy(300):
            with self.assertRaises(ValueError):
                self.query(max_attempts=1)  # no durable sink
            with client.record_requests(lambda event: None):
                with self.assertRaises(ValueError):
                    self.query(max_attempts=3)
        self.opener.open.assert_not_called()


class HistoricalFallbackTests(unittest.TestCase):
    """Recorded policies are evidence, independent of the new writer default."""
    setUp = fx.PipelineTests.setUp
    sleep = fx.PipelineTests.sleep
    open_response = fx.PipelineTests.open_response
    run_batch = BatchAuditTests.run_batch
    snapshot = BatchAuditTests.snapshot
    log_path = BatchAuditTests.log_path

    RECEIVED = '2026-11-08T13:56:12.956120Z'

    def historical(self, fallback=120.0, *, received=None, raw=None):
        received = received or self.RECEIVED
        stamp = client._parse_utc(received).timestamp()
        server = client._retry_deadline(raw, received)
        deadline = client._utc(max(client._parse_utc(server).timestamp(), stamp + 3)
                               if server is not None else stamp + fallback)
        return {
            'arxiv_retry_429': {'mode': 'retry_once', 'fallback_seconds': fallback},
            'arxiv_http_requests': [{
                'request_started_at_utc': received,
                'response_received_at_utc': received,
                'endpoint': 'https://export.arxiv.org/src/2508.07874v1',
                'query_identifier': '2508.07874v1', 'attempt': 2, 'http_status': 429,
                'retry_after': raw, 'cooldown_deadline_utc': server,
                'retry_wait_source': 'retry_after' if server is not None else 'internal_backoff',
                'retry_wait_deadline_utc': deadline,
                'retry_wait_seconds': max(3.0, client._parse_utc(deadline).timestamp() - stamp),
                'retry_429_decision': 'retry_exhausted',
            }],
        }

    def write_attempt(self, value, batch_id='old'):
        path = self.root.parent/'logs'/batch_id/'attempts'/'06e45d560cf148b78d856118f04f0989.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def check_at(self, timestamp):
        with patch.object(batch.time, 'time', return_value=timestamp):
            return batch.check_arxiv_cooldown(self.root.parent/'logs')

    def test_reported_120_second_log_blocks_until_135812_without_rewrite(self):
        self.assertEqual(client.RETRY_429_FALLBACK_SECONDS, 60)
        value = self.historical()
        event = value['arxiv_http_requests'][0]
        self.assertEqual(event['retry_wait_deadline_utc'], '2026-11-08T13:58:12.956120Z')
        self.write_attempt(value)
        before = self.snapshot()
        stamp = client._parse_utc(self.RECEIVED).timestamp()
        # Sixty seconds have already elapsed; historical 120 seconds have not.
        with self.assertRaises(client.ArxivCooldownError) as caught:
            self.check_at(stamp + 90)
        self.assertEqual(caught.exception.retry_at_utc, event['retry_wait_deadline_utc'])
        self.check_at(stamp + 121)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.requests, [])

    def test_one_log_root_accepts_60_and_120_and_keeps_latest_deadline(self):
        stamp = client._parse_utc(self.RECEIVED).timestamp()
        self.write_attempt(self.historical(120), 'old120')
        self.write_attempt(self.historical(60, received=client._utc(stamp + 3)), 'new60')
        before = self.snapshot()
        with self.assertRaises(client.ArxivCooldownError) as caught:
            self.check_at(stamp + 90)
        self.assertEqual(caught.exception.retry_at_utc, client._utc(stamp + 120))
        self.check_at(stamp + 121)
        self.assertEqual(before, self.snapshot())

    def test_missing_or_invalid_recorded_fallback_is_rejected_without_repair(self):
        cases = [None, True, False, 0, 1, -120, '120', {}, [], float('inf'), float('nan')]
        stamp = client._parse_utc(self.RECEIVED).timestamp()
        for bad in cases:
            with self.subTest(bad=bad):
                value = self.historical()
                value['arxiv_retry_429']['fallback_seconds'] = bad
                self.write_attempt(value)
                before = self.snapshot()
                with self.assertRaises(batch.BatchError):
                    self.check_at(stamp + 1000)
                self.assertEqual(before, self.snapshot())
        for remove_policy in (False, True):
            value = self.historical()
            if remove_policy:
                del value['arxiv_retry_429']
            else:
                del value['arxiv_retry_429']['fallback_seconds']
            self.write_attempt(value)
            before = self.snapshot()
            with self.assertRaises(batch.BatchError):
                self.check_at(stamp + 1000)
            self.assertEqual(before, self.snapshot())
        self.assertEqual(self.requests, [])

    def test_deadline_source_or_policy_mismatch_remains_invalid(self):
        stamp = client._parse_utc(self.RECEIVED).timestamp()
        for changes in (
                {'retry_wait_deadline_utc': client._utc(stamp+60)},
                {'retry_wait_deadline_utc': client._utc(stamp+121)},
                {'retry_wait_source': 'retry_after'},
                {'cooldown_deadline_utc': client._utc(stamp+120)},
                {'retry_wait_deadline_utc': None}):
            with self.subTest(changes=changes):
                value = self.historical()
                value['arxiv_http_requests'][0].update(changes)
                self.write_attempt(value)
                before = self.snapshot()
                with self.assertRaises(batch.BatchError):
                    self.check_at(stamp+1000)
                self.assertEqual(before,self.snapshot())
        value = self.historical()
        value['arxiv_retry_429']['fallback_seconds'] = 60  # Deadline still says 120.
        self.write_attempt(value)
        before = self.snapshot()
        with self.assertRaises(batch.BatchError):
            self.check_at(stamp+1000)
        self.assertEqual(before,self.snapshot())

    def test_server_retry_after_takes_priority_over_both_fallback_policies(self):
        stamp = client._parse_utc(self.RECEIVED).timestamp()
        for fallback in (60,120):
            for raw in ('180',formatdate(stamp+180,usegmt=True),'0'):
                with self.subTest(fallback=fallback,raw=raw):
                    value = self.historical(fallback,raw=raw)
                    event = value['arxiv_http_requests'][0]
                    self.write_attempt(value)
                    before = self.snapshot()
                    deadline = client._parse_utc(event['retry_wait_deadline_utc']).timestamp()
                    with self.assertRaises(client.ArxivCooldownError) as caught:
                        self.check_at(deadline-0.5)
                    self.assertEqual(caught.exception.retry_at_utc,event['retry_wait_deadline_utc'])
                    self.check_at(deadline+0.5)
                    self.assertEqual(before,self.snapshot())
                    self.assertEqual(event['retry_wait_source'],'retry_after')
                    self.assertIsNotNone(event['cooldown_deadline_utc'])

    def test_legacy_events_without_experimental_retry_fields_still_readable(self):
        value = self.historical()
        del value['arxiv_retry_429']
        event = value['arxiv_http_requests'][0]
        for key in ('retry_wait_source','retry_wait_deadline_utc','retry_wait_seconds','retry_429_decision'):
            del event[key]
        self.write_attempt(value)
        before = self.snapshot()
        self.check_at(client._parse_utc(self.RECEIVED).timestamp()+1)
        self.assertEqual(before,self.snapshot())

    def test_resume_old_120_attempt_with_new_60_preserves_old_log_and_valid_source(self):
        # Generate a complete historical attempt/checkpoint with the old policy,
        # not merely a minimal guard fixture. HTTP and all clocks remain fake.
        self.outcomes = [fx.xml_response(),fx.http_error(429)]
        with patch.object(client,'RETRY_429_FALLBACK_SECONDS',120.0), client.retry_429_policy(0):
            stopped = self.run_batch()
        self.assertEqual(stopped['status'],'stopped')
        old_path = self.log_path()
        old_bytes = old_path.read_bytes()
        self.assertEqual(stopped['arxiv_retry_429']['fallback_seconds'],120)
        self.assertEqual(client.RETRY_429_FALLBACK_SECONDS,60)
        before = self.snapshot()
        self.now += 90
        with patch.object(client,'_next_request_at',0.0):
            with self.assertRaises(client.ArxivCooldownError):
                self.run_batch(resume=True)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(len(self.requests),2)
        self.now += 31
        self.outcomes = [fx.http_error(429),fx.Response(fx.TEX)]
        with patch.object(client,'_next_request_at',0.0), client.retry_429_policy(300):
            resumed = self.run_batch(resume=True)
        self.assertEqual(resumed['status'],'finished')
        self.assertEqual(resumed['arxiv_retry_429']['fallback_seconds'],60)
        self.assertEqual(resumed['arxiv_retry_429']['waited_seconds'],60)
        self.assertEqual(resumed['papers'][0]['result']['discovery']['action'],'cached')
        self.assertEqual(len(self.requests),4)
        self.assertEqual(self.requests[1],self.requests[2])
        self.assertEqual(old_bytes,old_path.read_bytes())
        store = CorpusStorage(self.root)
        self.assertTrue(store.should_skip_version(*fx.PAIR,'v1'))
        source = (store.paper_path(*fx.PAIR)/'v1/main.tex')
        saved_source = source.read_bytes()
        cached = self.run_batch(resume=True)
        self.assertEqual(cached['arxiv_http_requests'],[])
        self.assertEqual(len(self.requests),4)
        self.assertEqual(saved_source,source.read_bytes())
        self.assertEqual(old_bytes,old_path.read_bytes())

if __name__ == '__main__':
    unittest.main()
