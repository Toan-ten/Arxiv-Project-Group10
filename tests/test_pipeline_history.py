"""Automatic revision history: real integration, fake HTTP and TEMP only."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_pipeline as fx
from test_arxiv_history import entry, DATES
from test_s2_integration import S2IntegrationTests as _Fixtures
from src import arxiv_history, batch, benchmark, benchmark_runner, pipeline, storage

PAIR, OTHER, THIRD = fx.PAIR, fx.OTHER, fx.THIRD


class HistoryIntegrationTests(unittest.TestCase):
    setUp = _Fixtures.setUp
    sleep = _Fixtures.sleep
    open_response = _Fixtures.open_response
    s2_open = _Fixtures.s2_open
    close_remaining = _Fixtures.close_remaining
    store = _Fixtures.store
    manifest = _Fixtures.manifest
    paper_path = _Fixtures.paper_path
    snapshot = _Fixtures.snapshot
    seed = _Fixtures.seed
    s2_success = _Fixtures.s2_success
    config = _Fixtures.config

    def run_pipeline(self, pairs=None, **options):
        settings = dict(timeout=15, max_attempts=1, run_id='history-offline')
        settings.update(options)
        # Exercise the production default; do not set revision_history here.
        return pipeline.run_selected([PAIR] if pairs is None else pairs, self.root, **settings)

    def run_batch(self, **options):
        settings = dict(batch_id='history-offline', max_ids=1,
                        log_root=self.root.parent / 'logs', timeout=15, max_attempts=1)
        settings.update(options)
        return batch.run_batch([PAIR], self.root, **settings)

    def history_responses(self, pair=PAIR, n=3):
        return [fx.Response(entry(f'v{i}', pair[1]), 'application/atom+xml')
                for i in range(1, n + 1)]

    def success(self, pair=PAIR, n=3, *, history=True):
        self.outcomes.extend([fx.xml_response(pair[1], n)] + [fx.Response(fx.TEX) for _ in range(n)])
        if history and n > 1:
            self.outcomes.extend(self.history_responses(pair, n))

    def checkpoint(self, name='metadata.json'):
        return (self.paper_path() / name).read_bytes()

    def assert_dates(self):
        metadata = self.store().read_metadata(*PAIR)
        self.assertEqual(metadata['revised_dates'], DATES[1:])
        self.assertEqual([v['date'] for v in metadata['versions']], DATES)
        self.assertTrue(all(v['date_status'] == 'verified' for v in metadata['versions']))
        self.assertEqual(metadata['mssv'], PAIR[0])

    def test_v1_uses_verified_discovery_date_without_extra_request(self):
        self.success(n=1)
        result = self.run_pipeline()['papers'][0]
        self.assertEqual(result['revision_history']['action'], 'cached')
        self.assertEqual(result['revision_history']['status'], 'complete')
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.store().read_metadata(*PAIR)['revised_dates'], [])
        self.assertEqual(result['metadata_status'], 'partial')
        self.assertEqual(result['references_status'], 'not_attempted')
        self.assertNotEqual(result['paper_status'], 'complete')

    def test_v1_missing_submission_can_be_filled_by_exact_version_response(self):
        xml = fx.atom().replace(f'<published>{fx.DATE}</published>'.encode(), b'')
        self.outcomes = [fx.Response(xml, 'application/atom+xml'), fx.Response(fx.TEX),
                         fx.Response(entry(), 'application/atom+xml')]
        result = self.run_pipeline()['papers'][0]
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(result['revision_history']['action'], 'queried')
        self.assertEqual(self.store().read_metadata(*PAIR)['submission_date'], fx.DATE)
        self.assertEqual(self.store().read_metadata(*PAIR)['revised_dates'], [])

    def test_three_versions_are_enriched_automatically_with_shared_pacing(self):
        self.success()
        result = self.run_pipeline()['papers'][0]
        self.assert_dates()
        self.assertEqual(len(self.requests), 7)
        self.assertEqual(self.starts, [0, 3, 6, 9, 12, 15, 18])
        self.assertEqual(result['revision_history']['received_versions'], [PAIR[1]+v for v in ('v1','v2','v3')])
        self.assertEqual(result['revision_history']['provider'], 'arxiv_atom_versioned')
        self.assertTrue(result['revision_history']['retrieved_at'])
        self.assertEqual(result['metadata_status'], 'partial')
        self.assertEqual(result['references_status'], 'not_attempted')
        for version in ('v1','v2','v3'):
            self.assertTrue(self.store().should_skip_version(*PAIR, version))
            self.assertEqual((self.paper_path()/version/'main.tex').read_bytes(), fx.TEX)

    def test_verified_history_resume_is_zero_http_and_keeps_metadata_bytes(self):
        self.success()
        self.run_pipeline()
        before, count = self.checkpoint(), len(self.requests)
        second = self.run_pipeline()['papers'][0]
        self.assertEqual(len(self.requests), count)
        self.assertEqual(self.checkpoint(), before)
        self.assertEqual(second['revision_history']['action'], 'cached')
        self.assertEqual(set(second['skipped']), {'v1','v2','v3'})
        self.assertEqual(second['discovery']['action'], 'cached')

    def test_timeout_between_history_versions_keeps_source_and_unknown_dates(self):
        self.success(history=False)
        self.outcomes.extend([self.history_responses()[0], TimeoutError('fixture timeout')])
        first = self.run_pipeline()['papers'][0]
        self.assertEqual(first['revision_history']['status'], 'partial')
        self.assertEqual(first['revision_history']['error']['type'], 'timeout')
        self.assertEqual(first['revision_history']['received_versions'], [PAIR[1]+'v1'])
        metadata = self.store().read_metadata(*PAIR)
        self.assertIsNone(metadata['revised_dates'])
        self.assertIsNone(metadata['provenance']['version_history'])
        self.assertTrue(all(v['date'] is None for v in metadata['versions'][1:]))
        self.assertEqual(metadata['status'], 'partial')
        source = {p: p.read_bytes() for p in self.paper_path().rglob('*.tex')}
        count = len(self.requests)
        self.outcomes = self.history_responses()
        resumed = self.run_pipeline()['papers'][0]
        self.assert_dates()
        self.assertEqual(len(self.requests)-count, 3)  # history retried, sources skipped
        self.assertEqual(set(resumed['skipped']), {'v1','v2','v3'})
        self.assertTrue(all(p.read_bytes()==body for p,body in source.items()))

    def test_invalid_dates_or_identity_do_not_create_revisions(self):
        responses = [
            entry('v2', date='2025'), entry('v3'),
            entry('v2', date='2025-02-13T10:00:00'),
            entry('v2', date='9999-12-31T23:59:59-23:59'),
            entry('v2').replace(b'<entry>', b'<link rel="self" href="http://["/><entry>'),
        ]
        self.success(history=False)
        self.outcomes.extend([self.history_responses()[0], fx.Response(responses[0], 'application/atom+xml')])
        self.run_pipeline()
        before = self.checkpoint()
        for xml in responses[1:]:
            with self.subTest(xml=xml[:80]):
                self.outcomes = [self.history_responses()[0], fx.Response(xml, 'application/atom+xml')]
                result = self.run_pipeline()['papers'][0]
                self.assertEqual(result['revision_history']['error']['type'], 'invalid_response')
                self.assertEqual(self.checkpoint(), before)
                self.assertIsNone(self.store().read_metadata(*PAIR)['revised_dates'])

    def test_version_selection_keeps_other_sources_pending_but_collects_all_dates(self):
        self.outcomes = [fx.xml_response(PAIR[1], 3), fx.Response(fx.TEX)] + self.history_responses()
        result = self.run_pipeline(versions=('v1',))['papers'][0]
        self.assert_dates()
        self.assertEqual(result['pending'], ['v2','v3'])
        self.assertEqual(len(self.requests), 5)
        self.assertFalse((self.paper_path()/'v2').exists())
        self.assertFalse((self.paper_path()/'v3').exists())

    def test_explicit_history_off_preserves_bounded_two_request_pilot(self):
        self.outcomes = [fx.xml_response(PAIR[1], 3), fx.Response(fx.TEX)]
        result = self.run_pipeline(versions=('v1',), revision_history=False)['papers'][0]
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn('revision_history', result)
        self.assertIsNone(self.store().read_metadata(*PAIR)['revised_dates'])

    def test_no_history_requests_for_not_found_unresolved_or_unknown_selection(self):
        for kind in ('not_found', 'unresolved', 'unknown_selection'):
            with self.subTest(kind=kind):
                pair = {'not_found': PAIR, 'unresolved': OTHER, 'unknown_selection': THIRD}[kind]
                xml = (b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/"><o:totalResults>0</o:totalResults></feed>'
                       if kind=='not_found' else fx.atom(pair[1], None if kind=='unresolved' else 3))
                self.outcomes = [fx.Response(xml, 'application/atom+xml')]
                count = len(self.requests)
                self.run_pipeline([pair], versions=('v99',) if kind=='unknown_selection' else None)
                self.assertEqual(len(self.requests)-count, 1)

    def test_history_precedes_s2_and_can_complete_all_contract_stages(self):
        self.success()
        self.s2_success()
        def before_metadata(request, timeout):
            self.assert_dates()
            return self.s2_open(request, timeout)
        self.transport.open.side_effect = before_metadata
        result = self.run_pipeline(s2_config=self.config())['papers'][0]
        self.assertEqual(result['paper_status'], 'complete')
        self.assertEqual(result['metadata_status'], 'complete')
        self.assertEqual(result['references_status'], 'complete')
        self.assertEqual(len(self.s2_requests), 2)

    def test_existing_s2_snapshots_and_sources_preserved_while_dates_are_enriched(self):
        self.success(history=False)
        self.s2_success()
        self.run_pipeline(s2_config=self.config(), revision_history=False)
        store = self.store()
        before_s2 = copy.deepcopy(store.read_metadata(*PAIR)['provenance']['semantic_scholar'])
        references, count = self.checkpoint('references.json'), len(self.s2_requests)
        sources = {p:p.read_bytes() for p in self.paper_path().rglob('*.tex')}
        self.outcomes = self.history_responses()
        result = self.run_pipeline(s2_config=self.config())['papers'][0]
        self.assert_dates()
        self.assertEqual(result['metadata_status'], 'complete')
        self.assertEqual(result['paper_status'], 'complete')
        self.assertEqual(store.read_metadata(*PAIR)['provenance']['semantic_scholar'], before_s2)
        self.assertEqual(self.checkpoint('references.json'), references)
        self.assertEqual(len(self.s2_requests), count)
        self.assertEqual(result['s2_metadata']['action'], 'cached')
        self.assertEqual(result['s2_references']['action'], 'cached')
        self.assertTrue(all(p.read_bytes()==body for p,body in sources.items()))

    def test_history_failure_preserves_verified_s2_provenance_and_complete_references(self):
        self.success(history=False)
        self.s2_success()
        self.run_pipeline(s2_config=self.config(), revision_history=False)
        metadata, references = self.checkpoint(), self.checkpoint('references.json')
        self.outcomes = [TimeoutError('fixture history timeout')]
        count = len(self.s2_requests)
        result = self.run_pipeline(s2_config=self.config())['papers'][0]
        self.assertEqual(self.checkpoint(), metadata)
        self.assertEqual(self.checkpoint('references.json'), references)
        self.assertEqual(len(self.s2_requests), count)
        self.assertEqual(result['metadata_status'], 'partial')
        self.assertNotEqual(result['paper_status'], 'complete')

    def test_invalid_history_cache_blocks_paper_without_s2_or_requery(self):
        self.success()
        self.s2_success()
        self.run_pipeline(s2_config=self.config())
        path=self.paper_path()/'metadata.json'
        value=json.loads(path.read_text())
        value['provenance']['version_history']['snapshot'][0]['date']=DATES[1]
        path.write_text(json.dumps(value),encoding='utf-8')
        before,count=path.read_bytes(),(len(self.requests),len(self.s2_requests))
        result=self.run_pipeline(s2_config=self.config())['papers'][0]
        self.assertEqual(result['revision_history']['action'],'blocked')
        self.assertIn('revision_history',result['blocked'])
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual((len(self.requests),len(self.s2_requests)),count)

    def test_damaged_source_prevents_history_and_s2_requests(self):
        self.success()
        self.run_pipeline()
        (self.paper_path()/'v2/main.tex').write_bytes(b'broken fixture')
        count=len(self.requests),len(self.s2_requests)
        result=self.run_pipeline(s2_config=self.config())['papers'][0]
        self.assertEqual(result['revision_history']['action'],'blocked')
        self.assertEqual((len(self.requests),len(self.s2_requests)),count)
        self.assertEqual((self.paper_path()/'v2/main.tex').read_bytes(),b'broken fixture')

    def test_history_save_failure_stops_before_s2_and_next_paper(self):
        self.success()
        with patch.object(storage.CorpusStorage,'save_metadata',side_effect=OSError('fixture disk failure')):
            with self.assertRaises(pipeline.PipelineStopped) as raised:
                self.run_pipeline([PAIR,OTHER],s2_config=self.config())
        self.assertEqual(len(raised.exception.summary['papers']),1)
        self.assertEqual(raised.exception.summary['papers'][0]['revision_history']['status'],'failed')
        self.assertEqual(len(self.s2_requests),0)
        self.assertEqual(len(self.requests),7)
        self.assertIsNone(self.store().read_metadata(*PAIR)['revised_dates'])
        for version in ('v1','v2','v3'):
            self.assertTrue(self.store().should_skip_version(*PAIR,version))

    def test_history_403_429_stop_before_s2_or_next_paper(self):
        for code,pair in ((403,PAIR),(429,OTHER)):
            with self.subTest(code=code):
                self.success(pair,history=False)
                self.outcomes.extend([self.history_responses(pair)[0],fx.http_error(code)])
                count=len(self.requests)
                result=self.run_pipeline([pair,THIRD],s2_config=self.config())
                self.assertTrue(result['stopped'])
                self.assertEqual(len(result['papers']),1)
                self.assertEqual(result['stop_reason']['stage'],'revision_history')
                self.assertEqual(result['stop_reason']['http_status'],code)
                self.assertEqual(result['stop_reason']['version'],'v2')
                self.assertFalse(result['stop_reason']['checkpoint_saved'])
                self.assertEqual(result['papers'][0]['revision_history']['status'],'partial')
                self.assertEqual(len(self.requests)-count,6)
                self.assertEqual(len(self.s2_requests),0)
                self.assertIsNone(self.store().read_metadata(*pair)['revised_dates'])

    def test_batch_stops_at_middle_paper_history_and_logs_reason(self):
        self.success(n=1)
        self.success(OTHER,history=False)
        self.outcomes.extend([self.history_responses(OTHER)[0],fx.http_error(429)])
        result=batch.run_batch([PAIR,OTHER,THIRD],self.root,batch_id='stop-history',max_ids=3,
                               log_root=self.root.parent/'logs',timeout=15,max_attempts=1)
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(result['papers'][2]['state'],'pending')
        self.assertEqual(result['stop_reason']['stage'],'revision_history')
        self.assertEqual(result['stats']['http_requests'],8)
        self.assertEqual(len(self.requests),8)
        self.assertTrue(self.store().should_skip_version(*PAIR,'v1'))
        for version in ('v1','v2','v3'):
            self.assertTrue(self.store().should_skip_version(*OTHER,version))
        path=next((self.root.parent/'logs/stop-history/attempts').glob('*.json'))
        self.assertEqual(json.loads(path.read_text())['stop_reason'],result['stop_reason'])
        audit=benchmark_runner.aggregate_batch(self.root,batch_id='stop-history',log_root=self.root.parent/'logs')
        self.assertEqual(audit['history']['attempts'][0]['stop_reason']['stage'],'revision_history')
        self.assertIn('revision_history',[e['stage'] for e in audit['history']['error_history']])

    def test_nonpolicy_history_timeout_allows_independent_next_paper(self):
        self.success(history=False)
        self.outcomes.append(TimeoutError('fixture history timeout'))
        self.success(OTHER,n=1)
        result=self.run_pipeline([PAIR,OTHER])
        self.assertFalse(result['stopped'])
        self.assertEqual(len(result['papers']),2)
        self.assertEqual(result['papers'][1]['source']['v1']['status'],'success')
        self.assertEqual(result['papers'][0]['revision_history']['status'],'failed')

    def test_batch_resume_after_interrupted_history_keeps_sources(self):
        self.success(history=False)
        self.outcomes.extend([self.history_responses()[0],KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.run_batch()
        path=next((self.root.parent/'logs/history-offline/attempts').glob('*.json'))
        old_log=path.read_bytes()
        self.assertEqual(json.loads(old_log)['status'],'interrupted')
        self.assertIsNone(self.store().read_metadata(*PAIR)['revised_dates'])
        source={p:p.read_bytes() for p in self.paper_path().rglob('*.tex')}
        self.outcomes=self.history_responses()
        count=len(self.requests)
        resumed=self.run_batch(resume=True)
        self.assert_dates()
        self.assertEqual(len(self.requests)-count,3)
        self.assertEqual(set(resumed['papers'][0]['result']['skipped']),{'v1','v2','v3'})
        self.assertTrue(all(p.read_bytes()==body for p,body in source.items()))
        self.assertEqual(path.read_bytes(),old_log)

    def test_new_batch_persists_history_enabled_and_resume_inherits_it(self):
        self.success()
        first=self.run_batch()
        definition=json.loads((self.root.parent/'logs/history-offline/batch.json').read_text())['definition']
        self.assertIs(definition['revision_history'],True)
        self.assertEqual(first['stats']['http_requests'],7)
        old_log=next((self.root.parent/'logs/history-offline/attempts').glob('*.json')).read_bytes()
        second=self.run_batch(resume=True)
        self.assertEqual(second['stats']['http_requests'],0)
        self.assertEqual(second['papers'][0]['result']['revision_history']['action'],'cached')
        self.assertEqual(len(self.requests),7)
        self.assertTrue(any(p.read_bytes()==old_log for p in (self.root.parent/'logs/history-offline/attempts').glob('*.json')))

    def test_legacy_batch_resumes_without_silently_enabling_history(self):
        self.success(history=False)
        self.run_batch(revision_history=False)
        definition=json.loads((self.root.parent/'logs/history-offline/batch.json').read_text())['definition']
        self.assertNotIn('revision_history',definition)
        old_logs={p:p.read_bytes() for p in (self.root.parent/'logs').rglob('*.json')}
        resumed=self.run_batch(resume=True)
        self.assertNotIn('revision_history',resumed['papers'][0]['result'])
        self.assertEqual(len(self.requests),4)
        with self.assertRaises(batch.BatchError):
            self.run_batch(resume=True,revision_history=True)
        self.assertTrue(all(p.read_bytes()==body for p,body in old_logs.items()))
        self.outcomes=self.history_responses()
        self.run_batch(batch_id='new-history-phase',revision_history=True)
        self.assert_dates()
        self.assertEqual(len(self.requests),7)

    def test_changed_history_setting_is_rejected_before_requests_or_log_writes(self):
        self.success()
        self.run_batch()
        before={p:p.read_bytes() for p in (self.root.parent/'logs').rglob('*.json')}
        with self.assertRaises(batch.BatchError):
            self.run_batch(resume=True,revision_history=False)
        self.assertEqual(len(self.requests),7)
        self.assertTrue(all(p.read_bytes()==body for p,body in before.items()))

    def engine(self,call,*,report_path,**options):
        start=self.now
        call()
        value=dict(status='success',elapsed_seconds=self.now-start,
                   peak_ram_mb=1,ram_source='offline-fixture',disk_corpus_size_mb=0.01)
        Path(report_path).write_text(json.dumps(value),encoding='utf-8')
        return value

    def benchmark(self,**options):
        settings=dict(batch_id='history-offline',max_ids=1,
                      log_root=self.root.parent/'logs',report_root=self.root.parent/'reports',
                      timeout=15,max_attempts=1)
        settings.update(options)
        with patch.object(benchmark,'benchmark_pipeline',side_effect=self.engine),patch.object(pipeline.time,'perf_counter',lambda:self.now):
            return benchmark_runner.run_benchmark([PAIR],self.root,**settings)

    def test_benchmark_wraps_history_time_metrics_and_provenance_without_new_events(self):
        self.success()
        value=self.benchmark()
        self.assertEqual(value['batch_status'],'finished')
        self.assertIs(value['configuration']['revision_history'],True)
        self.assertEqual(value['resource_metrics']['elapsed_seconds'],18)
        self.assertEqual(value['aggregation']['history']['http_requests'],7)
        path=next((self.root.parent/'logs/history-offline/attempts').glob('*.json'))
        result=json.loads(path.read_text())['papers'][0]['result']
        self.assertEqual(result['revision_history']['wall_seconds'],9)
        self.assertEqual(value['aggregation']['status'],'verified')
        self.assertFalse(value['publication_complete']['all_complete'])
        self.assertEqual(value['data_coverage']['valid_tex_versions'],3)
        second=self.benchmark(resume=True)
        self.assertEqual(second['resource_metrics']['elapsed_seconds'],0)
        self.assertEqual(second['aggregation']['history']['http_requests'],7)
        self.assertEqual(len(self.requests),7)

    def test_benchmark_can_read_and_resume_legacy_logs(self):
        self.success(history=False)
        self.run_batch(revision_history=False)
        value=self.benchmark(resume=True)
        self.assertEqual(value['batch_status'],'finished')
        self.assertNotIn('revision_history',value['configuration'])
        self.assertEqual(len(self.requests),4)
        self.assertEqual(value['aggregation']['status'],'verified')

    def test_invalid_history_option_is_rejected_before_writes_or_requests(self):
        for value in ([],{},1,'true'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.run_pipeline(revision_history=value)
                with self.assertRaises(batch.BatchError):
                    self.run_batch(revision_history=value)
                with self.assertRaises(benchmark_runner.BenchmarkError):
                    self.benchmark(revision_history=value)
        self.assertEqual(self.requests,[])
        self.assertFalse(self.root.exists())
        self.assertFalse((self.root.parent/'logs').exists())
        self.assertFalse((self.root.parent/'reports').exists())

    def test_only_the_existing_two_paper_callbacks_are_used(self):
        self.success()
        events=[]
        self.run_pipeline(on_progress=lambda event,summary:events.append(event))
        self.assertEqual(events,['paper_started','paper_finished'])


del _Fixtures


if __name__ == '__main__':
    unittest.main()
