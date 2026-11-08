"""arXiv HTTP audit and restarted-runtime admission; all HTTP is fake."""
import copy
from datetime import datetime, timezone
from email.utils import formatdate
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_arxiv_client as cf
import test_pipeline as fx
import test_m1_production as pf
from src import arxiv_client as client, batch, benchmark_runner
from src.storage import CorpusStorage
from src.pipeline import PipelineStopped

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("production_cooldown", ROOT / "scripts/m1_production.py")
production = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(production)


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


class ProductionCooldownTests(unittest.TestCase):
    sleep = pf.ProductionRunTests.sleep
    open_response = pf.ProductionRunTests.open_response
    fake_benchmark = pf.ProductionRunTests.fake_benchmark
    operator = pf.ProductionRunTests.operator
    success = pf.ProductionRunTests.success
    snapshot = pf.ProductionRunTests.snapshot

    def setUp(self):
        swapped = patch.object(pf, 'production', production)
        swapped.start()
        self.addCleanup(swapped.stop)
        pf.ProductionRunTests.setUp(self)

    def test_new_runtime_blocks_before_benchmark_and_prints_utc_deadline(self):
        first = production.cohort_pairs(1)[0]
        self.outcomes = [fx.xml_response(first[1]), fx.http_error(429, '120')]
        self.run.run()
        before = self.snapshot()
        with patch.object(client, '_next_request_at', 0.0), patch.object(benchmark_runner, 'run_benchmark') as measured:
            with self.assertRaises(client.ArxivCooldownError) as caught:
                self.operator().run()
            measured.assert_not_called()
        self.assertIn(caught.exception.retry_at_utc, self.output.getvalue())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.requests), 2)

    def test_expired_production_resume_then_audit_does_not_repeat_good_source(self):
        first, second = production.cohort_pairs(1)
        self.outcomes = [fx.xml_response(first[1]), fx.http_error(429, '120')]
        self.run.run()
        self.now += 121
        self.outcomes = [fx.Response(fx.TEX), fx.xml_response(second[1]), fx.Response(fx.TEX)]
        with patch.object(client, '_next_request_at', 0.0):
            result = self.operator().run()[0]
        self.assertTrue(result['arxiv_phase_complete'])
        self.assertEqual(len(self.requests), 5)
        before = self.snapshot(self.run.corpus)
        self.assertEqual(self.operator().run()[0]['mode'], 'audit')
        self.assertEqual(before, self.snapshot(self.run.corpus))
        self.assertEqual(len(self.requests), 5)

    def test_reviewed_upgrade_keeps_campaign_bytes_and_records_both_commits(self):
        self.drive.mkdir()
        path = self.drive/'campaign.json'
        value = {**self.run.definition, 'code_commit': next(iter(production._COMPATIBLE_CAMPAIGNS))}
        path.write_text(json.dumps(value))
        before = path.read_bytes()
        self.success()
        result = self.operator().run()[0]
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result['campaign_code_commit'], value['code_commit'])
        self.assertEqual(result['executing_code_commit'], pf.COMMIT)

    def test_upgrade_does_not_allow_unknown_commit_or_changed_settings(self):
        self.drive.mkdir()
        path = self.drive/'campaign.json'
        for changed in ({'code_commit': 'b'*40},
                        {'code_commit': next(iter(production._COMPATIBLE_CAMPAIGNS)), 'timeout': 100.0}):
            path.write_text(json.dumps({**self.run.definition, **changed}))
            before = path.read_bytes()
            with self.assertRaises(production.ProductionError):
                self.operator()
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.requests, [])


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

    def test_missing_and_invalid_retry_after_use_separate_120_second_deadline(self):
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
                self.assertEqual(event['retry_wait_deadline_utc'], client._utc(1700000120))
                self.assertEqual(self.starts, [0, 120])
                self.assertEqual(policy.waited, 120)

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
        self.assertEqual(self.starts, [0, 120])
        self.assertEqual(events[-1]['retry_429_decision'], 'retry_exhausted')
        self.assertEqual(events[-1]['retry_wait_deadline_utc'], client._utc(1700000240))
        self.assertEqual(policy.waited, 120)

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
        with client.retry_429_policy(300) as policy, client.record_requests(events.append):
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
        result, _, policy = self.run_retry(budget=120)
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


class ExperimentalProductionRetryTests(unittest.TestCase):
    setUp = ProductionCooldownTests.setUp
    sleep = pf.ProductionRunTests.sleep
    open_response = pf.ProductionRunTests.open_response
    fake_benchmark = pf.ProductionRunTests.fake_benchmark
    operator = pf.ProductionRunTests.operator
    success = pf.ProductionRunTests.success
    snapshot = pf.ProductionRunTests.snapshot

    def attempts(self):
        return [json.loads(p.read_text(encoding='utf-8'))
                for p in sorted(self.run.logs.glob('*/attempts/*.json'))]

    def enable(self, **options):
        self.run = self.operator(retry_429=True, **options)
        return production.cohort_pairs(1)

    def test_source_retry_success_then_resume_and_audit_without_duplicate_source(self):
        first, second = self.enable()
        self.outcomes = [fx.xml_response(first[1]), fx.http_error(429), fx.Response(fx.TEX),
                         fx.xml_response(second[1]), fx.Response(fx.TEX)]
        observed = []
        original = self.sleep
        def sleep(delay):
            if delay >= 120:
                event = self.attempts()[0]['arxiv_http_requests'][-1]
                self.assertEqual(event['http_status'], 429)
                self.assertEqual(event['retry_wait_source'], 'internal_backoff')
                self.assertEqual(event['retry_429_decision'], 'retry_once')
                observed.append(True)
            original(delay)
        with patch.object(client.time, 'sleep', side_effect=sleep):
            result = self.run.run()[0]
        self.assertTrue(result['arxiv_phase_complete'])
        self.assertTrue(observed)
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(self.requests[1], self.requests[2])
        current = self.attempts()[0]
        self.assertEqual(current['http_metrics']['total']['attempts'], 5)
        self.assertEqual(current['http_metrics']['total']['retry_attempts'], 1)
        self.assertEqual(current['arxiv_retry_429']['waited_seconds'], 120)
        self.assertEqual(CorpusStorage(self.run.corpus).read_manifest(*first)['versions']['v1']['attempts'], 2)
        protected = {name:self.snapshot(getattr(self.run,name)) for name in ('corpus','logs','reports')}
        self.assertEqual(self.run.run()[0]['mode'], 'audit')
        self.assertEqual(len(self.requests), 5)
        for name, before in protected.items():
            self.assertEqual(before, self.snapshot(getattr(self.run,name)))
        self.assertFalse(result['aggregation']['publication_complete']['all_complete'])

    def test_discovery_429_then_200_is_retried_before_source(self):
        first, second = self.enable()
        self.outcomes = [fx.http_error(429, '5'), fx.xml_response(first[1]), fx.Response(fx.TEX),
                         fx.xml_response(second[1]), fx.Response(fx.TEX)]
        result = self.run.run()[0]
        self.assertTrue(result['arxiv_phase_complete'])
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertEqual(self.starts[:2], [0, 5])
        self.assertEqual(result['arxiv_retry_429']['waited_seconds'], 5)

    def test_repeated_429_keeps_good_paper_and_stops_next_version_and_cohort(self):
        first, second = self.enable()
        self.outcomes = [fx.xml_response(first[1]), fx.Response(fx.TEX),
                         fx.xml_response(second[1], versions=2),
                         fx.http_error(429, '5'), fx.http_error(429, '10')]
        result = self.run.run(1, 2)[0]
        self.assertEqual(result['batch_status'], 'stopped')
        self.assertEqual(len(self.requests), 5)
        store = CorpusStorage(self.run.corpus)
        self.assertTrue(store.should_skip_version(*first, 'v1'))
        manifest = store.read_manifest(*second)
        self.assertEqual(manifest['versions']['v1']['status'], 'rate_limited')
        self.assertEqual(manifest['versions']['v2']['status'], 'pending')
        self.assertFalse((self.run.logs/'m1-arxiv-0002').exists())

    def test_403_does_not_sleep_retry_or_visit_second_paper(self):
        self.enable()
        self.outcomes = [fx.http_error(403)]
        result = self.run.run()[0]
        self.assertEqual(result['batch_status'], 'stopped')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result['arxiv_retry_429']['waited_seconds'], 0)

    def test_budget_is_shared_across_cohorts_in_one_run(self):
        self.enable()
        with patch.object(production, 'COHORT_SIZE', 1):
            first, second = production.cohort_pairs(1)[0], production.cohort_pairs(2)[0]
            # Reconstruct immutable campaign for the test-only size=1.
            self.run = self.operator(retry_429=True)
            self.outcomes = [fx.xml_response(first[1]), fx.http_error(429, '120'), fx.Response(fx.TEX),
                             fx.xml_response(second[1]), fx.http_error(429, '200')]
            results = self.run.run(1, 3)
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]['arxiv_phase_complete'])
        self.assertEqual(results[1]['batch_status'], 'stopped')
        self.assertEqual(results[1]['arxiv_retry_429']['waited_seconds'], 120)
        self.assertTrue(results[1]['arxiv_retry_429']['budget_exhausted'])
        self.assertEqual(len(self.requests), 5)

    def test_restart_during_wait_preserves_deadline_then_requires_explicit_resume(self):
        first, second = self.enable()
        self.outcomes = [fx.xml_response(first[1]), fx.http_error(429)]
        def interrupt(delay):
            if delay >= 120:
                event = self.attempts()[0]['arxiv_http_requests'][-1]
                self.assertIsNone(event['cooldown_deadline_utc'])
                self.assertEqual(event['retry_wait_source'], 'internal_backoff')
                self.now += 20
                raise KeyboardInterrupt
            self.now += delay
        with patch.object(client.time, 'sleep', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run.run()
        self.assertEqual(len(self.requests), 2)
        before = self.snapshot()
        with patch.object(client, '_next_request_at', 0.0):
            with self.assertRaises(client.ArxivCooldownError):
                self.operator(retry_429=True).run()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.requests), 2)
        self.now += 101
        self.outcomes = [fx.Response(fx.TEX), fx.xml_response(second[1]), fx.Response(fx.TEX)]
        with patch.object(client, '_next_request_at', 0.0):
            result = self.operator(retry_429=True).run()[0]
        self.assertTrue(result['arxiv_phase_complete'])
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(result['mode'], 'resume')
        # Prior attempt bytes remain unchanged; resumed source request is not Discovery.
        self.assertEqual(self.requests[1], self.requests[2])
        for name, raw in before.items():
            if '/attempts/' in name:
                self.assertEqual(raw, self.snapshot()[name])

    def test_excessive_server_deadline_stops_new_runtime_before_benchmark(self):
        self.enable()
        self.outcomes = [fx.http_error(429, '301')]
        result = self.run.run()[0]
        self.assertEqual(result['batch_status'], 'stopped')
        self.assertEqual(self.attempts()[0]['arxiv_http_requests'][0]['retry_429_decision'], 'budget_exceeded')
        before = self.snapshot()
        with patch.object(client, '_next_request_at', 0.0), patch.object(benchmark_runner, 'run_benchmark') as measured:
            with self.assertRaises(client.ArxivCooldownError):
                self.operator(retry_429=True).run()
            measured.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_fallback_evidence_corruption_fails_closed_without_repair(self):
        self.enable(retry_wait_budget=0)
        self.outcomes = [fx.http_error(429)]
        self.run.run()
        path = next(self.run.logs.glob('*/attempts/*.json'))
        value = json.loads(path.read_text(encoding='utf-8'))
        value['arxiv_http_requests'][0]['retry_wait_deadline_utc'] = None
        path.write_text(json.dumps(value),encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(batch.BatchError):
            self.operator().run()
        self.assertEqual(before,self.snapshot())
        self.assertEqual(len(self.requests),1)

    def test_log_write_failure_before_sleep_stops_without_retry(self):
        self.enable()
        self.outcomes = [fx.http_error(429)]
        original = batch._save
        def fail(path,value):
            if value.get('arxiv_http_requests'):
                raise batch.BatchLogError('offline durable log failure')
            return original(path,value)
        with patch.object(batch,'_save',side_effect=fail):
            with self.assertRaises(batch.BatchLogError):
                self.run.run()
        self.assertEqual(len(self.requests),1)
        self.assertIsNone(client._RETRY_429.get())

    def test_baseline_campaign_bytes_and_definition_remain_compatible(self):
        self.success()
        original_commit = production._COMPATIBLE_CAMPAIGNS.copy().pop()
        with patch.object(production, '_code_state', return_value=(original_commit, '')):
            old = production.ProductionRun(self.repo, self.drive, original_commit, timeout=15)
            old.run()
        campaign = (self.drive/'campaign.json').read_bytes()
        self.assertEqual(self.operator(retry_429=True).run()[0]['mode'], 'audit')
        self.assertEqual((self.drive/'campaign.json').read_bytes(),campaign)

    def test_real_benchmark_includes_fake_sleep_once_not_added_again_by_adapter(self):
        first, second = self.enable()
        self.patches[-1].stop()  # Use actual foundation and Adapter, still fake HTTP/clock.
        self.outcomes = [fx.xml_response(first[1]),fx.http_error(429),fx.Response(fx.TEX),
                         fx.xml_response(second[1]),fx.Response(fx.TEX)]
        with patch.object(client.time,'perf_counter',lambda:self.now):
            result = self.run.run()[0]
        report = json.loads(Path(result['benchmark_report_path']).read_text(encoding='utf-8'))
        self.assertEqual(report['resource_metrics']['metrics_v2']['wall_seconds'],129)
        self.assertEqual(report['benchmark_metrics']['end_to_end_wall_seconds'],129)
        self.assertEqual(self.attempts()[0]['wall_seconds'],129)
        self.assertEqual(result['arxiv_retry_429']['waited_seconds'],120)

    def test_notebook_config_exposes_opt_in_and_budget_at_one_place(self):
        nb=json.loads((ROOT/'notebooks/m1_colab_arxiv_production.ipynb').read_text(encoding='utf-8'))
        cells=[c for c in nb['cells'] if c['cell_type']=='code']
        self.assertEqual(len(cells),2)
        text=''.join(cells[1]['source'])
        self.assertEqual(text.count('"retry_429":'),1)
        self.assertIn('"retry_429": False',text)
        self.assertIn('"retry_wait_budget": 300.0',text)
        self.assertIn('retry_429=CONFIG["retry_429"]',text)
        for cell in cells:
            self.assertIsNone(cell['execution_count'])
            self.assertEqual(cell['outputs'],[])
            compile(''.join(cell['source']),'<colab>','exec')


    def test_history_429_uses_same_limiter_and_only_retries_exact_version_query(self):
        first, second = self.enable()
        self.outcomes = [fx.xml_response(first[1], versions=2),
                         fx.Response(fx.TEX), fx.Response(fx.TEX),
                         fx.Response(pf.entry('v1', first[1]),'application/atom+xml'),
                         fx.http_error(429, '12'),
                         fx.Response(pf.entry('v2', first[1]),'application/atom+xml'),
                         fx.xml_response(second[1]),fx.Response(fx.TEX)]
        result=self.run.run()[0]
        self.assertTrue(result['arxiv_phase_complete'])
        self.assertEqual(len(self.requests),8)
        self.assertEqual(self.requests[4],self.requests[5])
        self.assertIn(first[1]+'v2',self.requests[5])
        self.assertEqual(self.starts[5]-self.starts[4],12)
        metadata=CorpusStorage(self.run.corpus).read_metadata(*first)
        self.assertEqual(metadata['revised_dates'],pf.DATES[1:2])
        self.assertTrue(all(v['date_status']=='verified' for v in metadata['versions']))

    def test_429_then_403_stops_without_next_paper_and_preserves_error(self):
        self.enable()
        self.outcomes=[fx.http_error(429,'3'),fx.http_error(403)]
        result=self.run.run()[0]
        self.assertEqual(result['batch_status'],'stopped')
        self.assertEqual(result['stop_reason']['http_status'],403)
        self.assertEqual(len(self.requests),2)
        self.assertEqual(self.requests[0],self.requests[1])
        self.assertEqual([e['http_status'] for e in self.attempts()[0]['arxiv_http_requests']],[429,403])


    def test_restart_preserves_three_second_pacing_for_valid_zero_retry_after(self):
        self.enable()
        self.outcomes=[fx.http_error(429,'0')]
        def interrupt(delay):
            self.assertEqual(delay,3)
            self.now+=0.5
            raise KeyboardInterrupt
        with patch.object(client.time,'sleep',side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run.run()
        event=self.attempts()[0]['arxiv_http_requests'][0]
        self.assertEqual(event['cooldown_deadline_utc'],client._utc(1700000000))
        self.assertEqual(event['retry_wait_deadline_utc'],client._utc(1700000003))
        before=self.snapshot()
        with patch.object(client,'_next_request_at',0.0):
            with self.assertRaises(client.ArxivCooldownError):
                self.operator(retry_429=True).run()
        self.assertEqual(before,self.snapshot())
        self.assertEqual(len(self.requests),1)

if __name__ == '__main__':
    unittest.main()

