"""Colab handoff guards and API wiring, entirely offline; no Drive emulation claim."""
import ast
import contextlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import tempfile
import unittest
from unittest.mock import Mock, patch

from src import arxiv_client, benchmark_runner, semantic_scholar
from src.storage import CorpusStorage


ASSETS = Path(__file__).resolve().parents[1]


class ColabNotebookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notebook = json.loads((ASSETS / 'notebooks/m1_colab_cpu_pilot.ipynb').read_text(encoding='utf-8'))
        cls.cells = {c['id']: ''.join(c['source']) for c in cls.notebook['cells']}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.ns = dict(Path=Path, os=os, json=json, platform=platform, importlib=importlib)
        exec(compile(self.cells['helpers'], '<colab helpers>', 'exec'), self.ns)
        self.ns.update(REPO=self.root/'repo', COHORT=self.root/'cohort',
            CORPUS=self.root/'cohort'/'corpus', LOGS=self.root/'cohort'/'logs',
            REPORTS=self.root/'cohort'/'reports', SCRATCH=self.root/'scratch',
            PAIRS=[('24120010','2508.07872'),('24120010','2508.12871'),('24120035','2507.16659')],
            BATCH_ID='offline-colab', CODE_COMMIT='a'*40, CPU_ONLY_CONFIRMED=True,
            SINGLE_WRITER_CONFIRMED=True, PREFLIGHT=dict(verified=True, probe_path='offline-fixture'),
            git_read=lambda *args: 'a'*40 if args == ('rev-parse','HEAD') else '')
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        for target in (arxiv_client._opener, semantic_scholar._opener):
            mocked = patch.object(target, 'open', side_effect=AssertionError('Network forbidden'))
            mocked.start()
            self.addCleanup(mocked.stop)
        blocked = patch('socket.create_connection', side_effect=AssertionError('Network forbidden'))
        blocked.start()
        self.addCleanup(blocked.stop)

    def execute(self, tag, *, enabled=False):
        tree = ast.parse(self.cells[tag])
        if enabled:
            self.assertIsInstance(tree.body[0], ast.Assign)
            tree.body[0].value = ast.Constant(value=True)
            ast.fix_missing_locations(tree)
        exec(compile(tree, '<colab ' + tag + '>', 'exec'), self.ns)

    @staticmethod
    def report(**changes):
        value = dict(execution_status='returned', batch_status='finished', stop_reason=None,
                     report_path='offline/report.json', benchmark_metrics=None,
                     timing_metrics=None, data_coverage=None,
                     aggregation=dict(history=dict(http_metrics=None)))
        value.update(changes)
        return value

    def test_all_code_compiles_and_notebook_has_no_executed_outputs(self):
        self.assertEqual(self.notebook['nbformat'], 4)
        for c in self.notebook['cells']:
            if c['cell_type'] == 'code':
                compile(''.join(c['source']), c['id'], 'exec')
                self.assertIsNone(c['execution_count'])
                self.assertEqual(c['outputs'], [])

    def test_default_pilot_and_resume_cells_never_invoke_runner(self):
        run = self.ns['run_attempt'] = Mock(side_effect=AssertionError('Must be dry'))
        self.execute('pilot')
        self.execute('resume')
        run.assert_not_called()

    def test_selection_preserves_three_verified_local_pilot_owners(self):
        self.assertEqual(self.ns['validate_selection'](self.ns['PAIRS'], self.ns['CORPUS']), self.ns['PAIRS'])

    def test_invalid_ownership_duplicates_and_large_cohort_rejected(self):
        for pairs in ([('24120232','2508.07872')], self.ns['PAIRS'][:1]*2,
                      [('24120010','2508.07872v1')], self.ns['PAIRS']*2, []):
            with self.subTest(pairs=pairs), self.assertRaises(ValueError):
                self.ns['validate_selection'](pairs,self.ns['CORPUS'])

    def test_output_inside_repository_or_nested_roots_rejected(self):
        fn = self.ns['validate_layout']
        with self.assertRaises(ValueError):
            fn(self.root/'repo', self.root/'repo'/'data', self.root/'logs',self.root/'reports',self.root/'scratch')
        with self.assertRaises(ValueError):
            fn(self.root/'repo', self.root/'data',self.root/'data'/'logs',self.root/'reports',self.root/'scratch')

    def test_drive_probe_uses_new_fixture_and_real_atomic_checksum_roundtrip(self):
        probe = self.ns['drive_probe'](self.root/'preflight')
        self.assertTrue(probe['verified'])
        self.assertTrue(probe['fixture'])
        self.assertFalse(self.ns['CORPUS'].exists())
        value=json.loads((Path(probe['probe_path'])/'checkpoint.json').read_text())
        self.assertEqual(value,dict(fixture=True, revision=2))
        second=self.ns['drive_probe'](self.root/'preflight')
        self.assertNotEqual(probe['probe_path'],second['probe_path'])

    def test_probe_failure_stops_without_success_marker(self):
        with patch('src.storage.os.replace',side_effect=OSError('fixture replace failure')):
            with self.assertRaises(OSError):
                self.ns['drive_probe'](self.root/'preflight')
        self.assertEqual(list(self.root.rglob('result.json')),[])

    def test_cpu_writer_and_preflight_guards_precede_benchmark(self):
        for name in ('CPU_ONLY_CONFIRMED','SINGLE_WRITER_CONFIRMED','PREFLIGHT'):
            before=self.ns[name]
            self.ns[name]=dict(verified=False) if name=='PREFLIGHT' else False
            with patch.object(benchmark_runner,'run_benchmark') as run:
                with self.subTest(name=name),self.assertRaises(RuntimeError):
                    self.ns['run_attempt'](resume=False)
                run.assert_not_called()
            self.ns[name]=before

    def test_first_pilot_refuses_existing_corpus_before_benchmark(self):
        corpus=self.ns['CORPUS']
        corpus.mkdir(parents=True)
        evidence=corpus/'existing.json'
        evidence.write_bytes(b'preserve existing data')
        with patch.object(benchmark_runner,'run_benchmark') as run:
            with self.assertRaises(RuntimeError):
                self.ns['run_attempt'](resume=False)
            run.assert_not_called()
        self.assertEqual(evidence.read_bytes(),b'preserve existing data')

    def test_dirty_code_is_rejected_before_benchmark(self):
        self.ns['git_read']=lambda *args: 'a'*40 if args==('rev-parse','HEAD') else ' M src/pipeline.py'
        with patch.object(benchmark_runner,'run_benchmark') as run:
            with self.assertRaises(RuntimeError):
                self.ns['run_attempt'](resume=False)
            run.assert_not_called()

    def test_actual_call_uses_all_versions_history_s2_off_and_unique_experiments(self):
        with patch.object(benchmark_runner,'run_benchmark',return_value=self.report()) as run:
            self.ns['run_attempt'](resume=False)
            self.ns['run_attempt'](resume=True)
        first, second = run.call_args_list
        self.assertEqual(first.args,(self.ns['PAIRS'],self.ns['CORPUS']))
        for call in (first,second):
            self.assertIsNone(call.kwargs['versions'])
            self.assertIsNone(call.kwargs['s2_config'])
            self.assertTrue(call.kwargs['revision_history'])
            self.assertEqual(call.kwargs['max_ids'],3)
            self.assertEqual(call.kwargs['max_attempts'],1)
            self.assertEqual(call.kwargs['disk_roots'],[self.ns['CORPUS'],self.ns['SCRATCH']])
        self.assertEqual(first.kwargs['batch_id'],second.kwargs['batch_id'])
        self.assertNotEqual(first.kwargs['experiment_id'],second.kwargs['experiment_id'])
        self.assertFalse(first.kwargs['resume'])
        self.assertTrue(second.kwargs['resume'])
        notes=list((self.ns['COHORT']/'sessions').glob('*.json'))
        self.assertEqual(len(notes),2)
        self.assertTrue(all('SEMANTIC_SCHOLAR_API_KEY' not in p.read_text() for p in notes))

    def test_policy_stop_raises_and_does_not_retry(self):
        for http in (403,429):
            report=self.report(batch_status='stopped',stop_reason=dict(http_status=http))
            with self.subTest(http=http),patch.object(benchmark_runner,'run_benchmark',return_value=report) as run:
                with self.assertRaises(RuntimeError):
                    self.ns['run_attempt'](resume=False)
                self.assertEqual(run.call_count,1)

    def test_benchmark_exception_is_not_retried_or_turned_into_success(self):
        with patch.object(benchmark_runner,'run_benchmark',side_effect=OSError('offline failure')) as run:
            with self.assertRaises(OSError):
                self.ns['run_attempt'](resume=False)
            self.assertEqual(run.call_count,1)

    def test_missing_history_or_checkpoint_does_not_pass_ready_check(self):
        audit=dict(status='verified',data_coverage=dict(all_version_sources_terminal=True))
        with patch.object(benchmark_runner,'aggregate_batch',return_value=audit):
            result=self.ns['audit_cohort']()
        self.assertFalse(result['ready_for_zero_http_resume'])
        self.assertTrue(all(not h['verified'] for h in result['revision_history']))

    def seed_discovery(self, count):
        store=CorpusStorage(self.ns['CORPUS'])
        for mssv,identifier in self.ns['PAIRS']:
            store.record_discovery(dict(mssv=mssv,arxiv_id=identifier,status='found',
                version_status='resolved',version_ids=[identifier+'v'+str(n) for n in range(1,count+1)],
                latest_version_id=identifier+'v'+str(count),title='Offline fixture',authors=['Fixture'],
                published='2025-08-11T11:43:34Z',updated='2025-08-11T11:43:34Z',missing_fields=[],
                http_status=200,attempts=1,error=None))

    def test_real_single_version_discovery_date_cache_is_audited_without_http(self):
        self.seed_discovery(1)
        audit=dict(status='verified',data_coverage=dict(all_version_sources_terminal=True))
        with patch.object(benchmark_runner,'aggregate_batch',return_value=audit):
            result=self.ns['audit_cohort']()
        self.assertTrue(result['ready_for_zero_http_resume'])
        self.assertTrue(all(h['verified'] for h in result['revision_history']))

    def test_missing_multi_version_history_never_passes_date_audit(self):
        self.seed_discovery(2)
        audit=dict(status='verified',data_coverage=dict(all_version_sources_terminal=True))
        with patch.object(benchmark_runner,'aggregate_batch',return_value=audit):
            result=self.ns['audit_cohort']()
        self.assertFalse(result['ready_for_zero_http_resume'])

    def run_resume_cell(self, *, http=0, changed=False):
        path=self.ns['LOGS']/self.ns['BATCH_ID']/'attempts'/'offline-attempt.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(http_metrics=dict(status='collected',total=dict(attempts=http)))))
        self.ns['audit_cohort']=Mock(return_value=dict(ready_for_zero_http_resume=True))
        self.ns['protected_hashes']=Mock(side_effect=[{'main.tex':'before'},{'main.tex':'changed' if changed else 'before'}])
        self.ns['run_attempt']=Mock(return_value=dict(attempt_ids=['offline-attempt']))
        self.execute('resume',enabled=True)

    def test_verified_resume_checks_current_attempt_not_cumulative_http(self):
        self.run_resume_cell()
        self.ns['run_attempt'].assert_called_once_with(resume=True)

    def test_verified_resume_detects_unexpected_http(self):
        with self.assertRaisesRegex(RuntimeError,'HTTP'):
            self.run_resume_cell(http=1)

    def test_verified_resume_detects_changed_metadata_or_source(self):
        with self.assertRaisesRegex(RuntimeError,'thay đổi'):
            self.run_resume_cell(changed=True)

    def test_no_batch_audit_does_not_fabricate_results(self):
        self.ns['audit_cohort']=Mock(side_effect=AssertionError('No batch'))
        self.execute('audit-export')
        self.ns['audit_cohort'].assert_not_called()
        self.assertFalse((self.ns['COHORT']/'handoff').exists())


if __name__ == '__main__':
    unittest.main()
