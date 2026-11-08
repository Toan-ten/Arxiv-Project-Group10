"""A/B controller/measurement tests; fake HTTP only, no Colab account required."""
import ast
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('comparison_under_test', ROOT/'scripts/colab_comparison.py')
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)
COMMIT = 'a'*40
KEY = 'offline-fixture-key-do-not-use'


def evidence(phase, n=1, *, wall=2, size=100, status='finished', requests=3):
    attempts, observations = [], []
    for index in range(n):
        identifier=f'{phase}-{index}'
        timing=dict(discovery_wall_seconds=1,known_paper_wall_seconds=50,
                    average_paper_status='collected')
        attempts.append(dict(attempt_id=identifier,status=status,timing_metrics=timing))
        resources=dict(wall_seconds=wall,ram=dict(status='collected',source='fake',scope='process tree',
            sample_count=2,sum_sample_bytes=30,sampled_peak_bytes=20),
            disk=dict(status='collected',roots=['/cohort/corpus','/cohort/scratch'],
                      sampled_peak_bytes=size+10,final_corpus_bytes=size))
        observations.append(dict(attempt_id=identifier,resources=resources,environment=dict(commit=COMMIT)))
    return dict(batch_id=phase,status='verified',history=dict(attempts=attempts,error_history=[],
        timings=dict(discovery_wall_seconds=n,known_paper_wall_seconds=50*n,average_paper_status='collected'),
        http_metrics=dict(known=dict(attempts=requests,status_counts={}),missing_attempt_ids=[],
                          invalid_attempt_ids=[],partial_attempt_ids=[])),
        benchmark_metrics=dict(attempts=observations),data_coverage=dict(found_papers=40,not_found_papers=10,
            valid_tex_versions=60,no_tex_versions=2,references_complete_papers=40),
        publication_complete=dict(papers=38,all_complete=False))


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        for target,args in [(cc,'code_state'),(cc,'runtime_profile')]:
            value=dict(commit=COMMIT,dirty=False) if args=='code_state' else dict(kernel='offline',python='fixture')
            p=patch.object(target,args,return_value=value)
            p.start(); self.addCleanup(p.stop)
        p=patch.object(cc.time,'sleep')
        p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,{'SEMANTIC_SCHOLAR_API_KEY':KEY})
        p.start();self.addCleanup(p.stop)
        p=patch('socket.create_connection',side_effect=AssertionError('Real network forbidden'))
        p.start();self.addCleanup(p.stop)
        self.session=cc.Comparison(self.root/'trial',self.root/'scratch',COMMIT,secret_loader=lambda:KEY)

    def receipt(self, *, ok=True, status=200):
        folder=self.session.root/'preflights';folder.mkdir(exist_ok=True)
        cc._atomic_json(folder/'fixture.json',dict(ok=ok,http_status=status,recorded_at='2026-11-08T00:00:00+00:00'))

    def report(self, *, status='finished'):
        return dict(execution_status='returned',batch_status=status,stop_reason=None,
                    report_path=str(self.root/'report.json'),attempt_ids=['fixture-attempt'])

    def test_cohort_is_50_unique_balanced_owned_and_deterministic_without_http(self):
        pairs=cc.select_cohort(10)
        self.assertEqual(pairs,cc.select_cohort(10))
        self.assertNotEqual(pairs,cc.select_cohort(11))
        self.assertEqual(len(pairs),len(set(pairs)))
        self.assertEqual(len(pairs),50)
        counts={owner:sum(p[0]==owner for p in pairs) for owner in cc.load_config()}
        self.assertEqual(sorted(counts.values()),[12,12,13,13])
        cc.pipeline._inputs(pairs,cc.CorpusStorage(self.root/'unused'),30,1,cc.arxiv_client.USER_AGENT)
        self.assertEqual(len(set(p[0] for p in pairs[:4])),4)
        self.assertFalse((self.root/'unused').exists())

    def test_constructor_does_not_load_secrets_or_call_client(self):
        loader=Mock(side_effect=AssertionError('Do not load on init'))
        with patch.object(cc.semantic_scholar,'fetch_metadata') as network:
            cc.Comparison(self.root/'other',self.root/'other-scratch',COMMIT,secret_loader=loader)
        loader.assert_not_called();network.assert_not_called()

    def test_existing_experiment_requires_exact_config_and_does_not_overwrite(self):
        before=(self.session.root/'experiment.json').read_bytes()
        with self.assertRaises(cc.ComparisonError):
            cc.Comparison(self.session.root,self.session.scratch,COMMIT,seed=11)
        self.assertEqual((self.session.root/'experiment.json').read_bytes(),before)

    def test_overlapping_or_repository_output_rejected(self):
        with self.assertRaises(cc.ComparisonError):
            cc.Comparison(self.root/'overlap',self.root/'overlap'/'scratch',COMMIT)
        with self.assertRaises(cc.ComparisonError):
            cc.Comparison(Path(cc.__file__).parents[1]/'data',self.root/'outside',COMMIT)

    def test_filesystem_probe_isolated_roundtrip(self):
        result=cc.filesystem_probe(self.root/'probe')
        self.assertTrue(result['verified']);self.assertTrue(result['fixture'])
        self.assertFalse(self.session.locations('A')['corpus'].exists())

    def test_preflight_real_client_fake_transport_one_post_four_ids_and_no_cache(self):
        papers=[dict(paperId='paper-'+str(i),externalIds=dict(ArXiv=p[1]))
                for i,p in enumerate(self.session.pairs[:4])]
        response=io.BytesIO(json.dumps(papers).encode())
        response.status=200;response.headers={'Content-Type':'application/json'}
        with patch.object(cc.semantic_scholar._opener,'open',return_value=response) as transport:
            receipt=self.session.preflight()
        self.assertTrue(receipt['ok'])
        self.assertEqual(transport.call_count,1)
        request=transport.call_args.args[0]
        self.assertEqual(request.get_method(),'POST')
        self.assertEqual(json.loads(request.data)['ids'],['ARXIV:'+p[1] for p in self.session.pairs[:4]])
        self.assertEqual(request.get_header('X-api-key'),KEY)
        self.assertEqual(receipt['http_metrics']['total']['attempts'],1)
        self.assertFalse(self.session.locations('A')['corpus'].exists())
        self.assertNotIn(KEY,json.dumps(receipt))

    def test_429_preflight_is_one_attempt_and_blocks_both_strategies(self):
        error=HTTPError('https://fixture.invalid',429,'fixture',{},io.BytesIO())
        with patch.object(cc.semantic_scholar._opener,'open',side_effect=error) as transport:
            receipt=self.session.preflight()
        self.assertFalse(receipt['ok']);self.assertEqual(receipt['http_status'],429)
        self.assertEqual(transport.call_count,1)
        for arm in ('A','B'):
            with self.assertRaises(cc.ComparisonError):self.session.run(arm)

    def test_preflight_response_identity_or_length_mismatch_cannot_authorize_50(self):
        rows=[dict(arxiv_id='2502.09992',status='found',http_status=200)]
        with patch.object(cc.semantic_scholar,'fetch_metadata',return_value=rows):
            self.assertFalse(self.session.preflight()['ok'])
        with self.assertRaises(cc.ComparisonError):self.session.run('A')

    def test_key_errors_are_sanitized_and_never_persisted(self):
        self.session.secret_loader=Mock(side_effect=RuntimeError(KEY))
        with self.assertRaises(cc.ComparisonError) as raised:self.session.preflight()
        self.assertNotIn(KEY,str(raised.exception))
        self.assertNotIn(KEY,(self.session.root/'experiment.json').read_text())

    def test_fresh_a_uses_real_adapter_interface_and_preserves_shared_config(self):
        self.receipt()
        with patch.object(cc.benchmark_runner,'run_benchmark',return_value=self.report()) as run:
            outcome=self.session.run('A')
        self.assertEqual(len(outcome),1)
        self.assertEqual(run.call_args.args[0],self.session.pairs)
        kw=run.call_args.kwargs
        self.assertEqual(kw['max_ids'],50);self.assertIsNone(kw['versions'])
        self.assertTrue(kw['revision_history']);self.assertEqual(kw['max_attempts'],1)
        self.assertEqual(kw['s2_config'],self.session.s2)
        self.assertFalse(kw['resume'])

    def test_b_runs_arxiv_then_s2_separate_ids_same_corpus(self):
        self.receipt()
        with patch.object(self.session,'_integrity',return_value=dict(ready=True)),patch.object(
                cc.benchmark_runner,'run_benchmark',return_value=self.report()) as run:
            self.session.run('B')
        first,second=run.call_args_list
        self.assertEqual(first.args,second.args)
        self.assertEqual([first.kwargs['batch_id'],second.kwargs['batch_id']],['arxiv','s2'])
        self.assertIsNone(first.kwargs['s2_config']);self.assertEqual(second.kwargs['s2_config'],self.session.s2)
        self.assertNotEqual(first.kwargs['experiment_id'],second.kwargs['experiment_id'])
        self.assertNotEqual(self.session.locations('A')['corpus'],self.session.locations('B')['corpus'])

    def test_b_does_not_start_s2_after_stop_or_incomplete_arxiv(self):
        self.receipt()
        for status in ('stopped','finished'):
            with self.subTest(status=status),patch.object(self.session,'_integrity',return_value=dict(ready=False)),patch.object(
                    cc.benchmark_runner,'run_benchmark',return_value=self.report(status=status)) as run:
                self.session.run('B')
                self.assertEqual(run.call_count,1)

    def test_a_stop_cannot_implicitly_start_b(self):
        self.receipt()
        audit=evidence('combined',status='stopped')
        with patch.object(self.session,'_audits',side_effect=lambda arm:[audit] if arm=='A' else []),patch.object(self.session,'_phase') as phase:
            with self.assertRaises(cc.ComparisonError):self.session.run('B')
            phase.assert_not_called()

    def test_no_preflight_tampered_selection_or_dirty_commit_cannot_send_http(self):
        with self.assertRaises(cc.ComparisonError):self.session.run('A')
        self.receipt()
        self.session.pairs.reverse()
        with self.assertRaises(cc.ComparisonError):self.session.run('A')
        self.session.pairs.reverse()
        with patch.object(cc,'code_state',return_value=dict(commit=COMMIT,dirty=True)):
            with self.assertRaises(cc.ComparisonError):self.session.run('A')

    def test_lock_rejects_overlapping_actions(self):
        with self.session._exclusive():
            with self.assertRaises(cc.ComparisonError):self.session.preflight()
            with self.assertRaises(cc.ComparisonError):self.session.run('B')
            with self.assertRaises(cc.ComparisonError):self.session.compare()

    def test_fresh_run_refuses_existing_source(self):
        self.receipt()
        path=self.session.locations('A')['corpus']/'existing.tex'
        path.parent.mkdir(parents=True);path.write_bytes(b'preserve fixture')
        with self.assertRaises(cc.ComparisonError):self.session.run('A')
        self.assertEqual(path.read_bytes(),b'preserve fixture')

    def resume_fixture(self, *, http=0, changed=False):
        self.receipt()
        paths=self.session.locations('B')
        source=paths['corpus']/'v1'/'main.tex'
        source.parent.mkdir(parents=True);source.write_bytes(b'original fixture')
        for phase in ('arxiv','s2'):
            folder=paths['logs']/phase;folder.mkdir(parents=True)
            (folder/'batch.json').write_text('{}')
        def phase(arm,name,*,resume):
            self.assertEqual((arm,name,resume),('B','s2',True))
            folder=paths['logs']/name/'attempts';folder.mkdir()
            cc._atomic_json(folder/'fixture.json',dict(http_metrics=dict(status='collected',total=dict(attempts=http))))
            if changed:source.write_bytes(b'corrupt fixture')
            return dict(phase='s2',execution_status='returned',batch_status='finished',
                        attempt_ids=['fixture'],report_path=str(self.root/'report.json'))
        with patch.object(self.session,'_audits',return_value=[evidence('arxiv'),evidence('s2')]),patch.object(
                self.session,'_integrity',return_value=dict(ready=True,cacheable=True,signature='same')),patch.object(
                self.session,'_phase',side_effect=phase) as run:
            result=self.session.run('B',resume=True)
            self.assertEqual(run.call_count,1)
        return result,source

    def test_resume_b_skips_arxiv_phase_keeps_source_and_checks_current_attempt_zero_http(self):
        outcomes,source=self.resume_fixture()
        self.assertEqual(len(outcomes),1)
        self.assertEqual(source.read_bytes(),b'original fixture')

    def test_verified_resume_detects_unexpected_http_or_modified_source(self):
        with self.assertRaises(cc.ComparisonError):self.resume_fixture(http=1)
        # Separate temporary experiment for the second scenario.
        self.session=cc.Comparison(self.root/'trial2',self.root/'scratch2',COMMIT,secret_loader=lambda:KEY)
        with self.assertRaises(cc.ComparisonError):self.resume_fixture(changed=True)

    def test_corrupt_checkpoint_blocks_before_another_request(self):
        self.receipt()
        bad=dict(status='incomplete',issues=[dict(code='invalid_checkpoint')])
        with patch.object(self.session,'_audits',return_value=[bad]),patch.object(self.session,'_phase') as phase:
            with self.assertRaises(cc.ComparisonError):self.session.run('A',resume=True)
            phase.assert_not_called()

    def test_metrics_sum_distinct_attempts_weighted_ram_max_peaks_last_disk(self):
        a,b=evidence('arxiv',wall=2,size=100),evidence('s2',wall=5,size=300)
        b['benchmark_metrics']['attempts'][0]['resources']['ram'].update(
            sample_count=1,sum_sample_bytes=30,sampled_peak_bytes=30)
        total=cc.resource_totals([a,b])
        self.assertEqual(total['wall_seconds'],7)
        self.assertEqual(total['average_ram_bytes'],20)
        self.assertEqual(total['peak_ram_bytes'],30)
        self.assertEqual(total['peak_disk_bytes'],310)
        self.assertEqual(total['final_corpus_bytes'],300)

    def test_duplicates_missing_measurements_and_scope_changes_not_full_metrics(self):
        a=evidence('combined')
        a['benchmark_metrics']['attempts']*=2
        total=cc.resource_totals([a]);self.assertIsNone(total['wall_seconds'])
        self.assertEqual(total['known_wall_seconds'],2)
        a=evidence('combined',n=2)
        a['benchmark_metrics']['attempts'].pop()
        self.assertFalse(cc.resource_totals([a])['resources_complete'])
        a,b=evidence('arxiv'),evidence('s2')
        b['benchmark_metrics']['attempts'][0]['resources']['disk']['roots']=['/different']
        self.assertIsNone(cc.resource_totals([a,b])['peak_disk_bytes'])

    def test_cold_and_resume_measurements_are_separate(self):
        audit=evidence('combined',n=2,wall=3)
        self.assertEqual(cc.resource_totals([audit],cold=True)['wall_seconds'],3)
        self.assertEqual(cc.resource_totals([audit])['wall_seconds'],6)

    def test_whole_action_wall_is_measured_separately_not_inferred_from_callback(self):
        self.receipt()
        outcome=dict(phase='combined',execution_status='returned',batch_status='finished',
                     attempt_ids=['fixture'],report_path='offline/report.json')
        with patch.object(self.session,'_phase',return_value=outcome),patch.object(
                self.session,'_integrity',return_value=dict(ready=True)),patch.object(
                cc.time,'perf_counter',side_effect=[100,115]):
            self.session.run('A')
        operation=json.loads(next((self.session.root/'operations').glob('*.json')).read_text())
        self.assertEqual(operation['wall_seconds'],15)
        self.assertEqual(operation['status'],'finished')

    def test_interrupted_action_preserves_unknown_interval_and_cannot_rank(self):
        folder=self.session.root/'operations';folder.mkdir()
        cc._atomic_json(folder/'running.json',dict(arm='A',resume=False,status='running',
            wall_seconds=None,started_at='2026-11-08T00:00:00+00:00'))
        with patch.object(self.session,'_audits',return_value=[evidence('combined')]),patch.object(
                self.session,'_integrity',return_value=dict(ready=True,signature='same')):
            row=self.session._row('A')
        self.assertIsNone(row['cold_end_to_end_seconds'])
        self.assertFalse(row['cold_eligible'])

    def test_all_not_found_does_not_claim_full_m1_speed_comparison(self):
        audit=evidence('combined');audit['data_coverage']['found_papers']=0
        folder=self.session.root/'operations';folder.mkdir()
        cc._atomic_json(folder/'finished.json',dict(arm='A',resume=False,status='finished',
            wall_seconds=10,started_at='2026-11-08T00:00:00+00:00'))
        with patch.object(self.session,'_audits',return_value=[audit]),patch.object(
                self.session,'_integrity',return_value=dict(ready=True,signature='same')):
            self.assertFalse(self.session._row('A')['cold_eligible'])

    def test_real_storage_not_found_integrity_and_corrupt_json_are_distinct(self):
        store=cc.CorpusStorage(self.session.locations('A')['corpus'])
        for owner,identifier in self.session.pairs:
            store.record_discovery(dict(mssv=owner,arxiv_id=identifier,status='not_found'))
        with patch.object(self.session,'_audits',return_value=[dict(status='verified')]):
            self.assertTrue(self.session._integrity('A',s2=True)['ready'])
            owner,identifier=self.session.pairs[0]
            (store.paper_path(owner,identifier)/'metadata.json').write_bytes(b'broken JSON')
            self.assertFalse(self.session._integrity('A',s2=True)['ready'])

    def test_changed_runtime_cannot_enable_speed_winner(self):
        folder=self.session.root/'sessions';folder.mkdir()
        for index in range(2):cc._atomic_json(folder/f'{index}.json',dict(runtime=dict(kernel=index)))
        rows=[dict(strategy=a,cold_eligible=True,data_signature='same',cold_end_to_end_seconds=n)
              for a,n in [('A',1),('B',2)]]
        with patch.object(self.session,'_row',side_effect=rows):
            report,_=self.session.compare()
        self.assertFalse(report['speed_comparison_eligible'])

    def test_row_b_does_not_average_twice_or_sum_coverage_snapshots(self):
        audits=[evidence('arxiv',requests=7),evidence('s2',requests=3)]
        with patch.object(self.session,'_audits',return_value=audits),patch.object(
                self.session,'_integrity',return_value=dict(ready=True,signature='same')):
            row=self.session._row('B')
        self.assertEqual(row['http_requests'],10)
        self.assertEqual(row['average_paper_seconds'],2)
        self.assertEqual(row['valid_tex_versions'],60)
        self.assertEqual(row['publication_complete_papers'],38)

    def test_missing_or_corrupt_logs_are_unknown_and_do_not_create_winner(self):
        report,destination=self.session.compare()
        self.assertIsNone(report['faster_observed_arm'])
        self.assertFalse(report['speed_comparison_eligible'])
        self.assertIsNone(report['rows'][0]['active_wall_seconds'])
        self.assertTrue((destination/'comparison.csv').is_file())

    def test_stopped_history_never_wins_after_successful_resume(self):
        a=evidence('combined',n=2)
        a['history']['attempts'][0]['status']='stopped'
        b=[evidence('arxiv'),evidence('s2')]
        sessions=self.session.root/'sessions';sessions.mkdir()
        cc._atomic_json(sessions/'one.json',dict(runtime=dict(kernel='same')))
        with patch.object(self.session,'_audits',side_effect=lambda arm:[a] if arm=='A' else b),patch.object(
                self.session,'_integrity',return_value=dict(ready=True,signature='same')):
            result,_=self.session.compare()
        self.assertFalse(result['speed_comparison_eligible']);self.assertIsNone(result['faster_observed_arm'])

    def test_complete_matching_trial_can_report_observation_without_claiming_m1_complete(self):
        a=evidence('combined',wall=3)
        b=[evidence('arxiv',wall=2),evidence('s2',wall=2)]
        sessions=self.session.root/'sessions';sessions.mkdir()
        cc._atomic_json(sessions/'one.json',dict(runtime=dict(kernel='same')))
        operations=self.session.root/'operations';operations.mkdir()
        for arm,seconds in [('A',3),('B',4)]:
            cc._atomic_json(operations/(arm+'.json'),dict(arm=arm,resume=False,status='finished',
                wall_seconds=seconds,started_at='2026-11-08T00:00:00+00:00'))
        with patch.object(self.session,'_audits',side_effect=lambda arm:[a] if arm=='A' else b),patch.object(
                self.session,'_integrity',return_value=dict(ready=True,signature='same')):
            result,first=self.session.compare()
            _,second=self.session.compare()
        self.assertEqual(result['faster_observed_arm'],'A')
        self.assertEqual(result['rows'][0]['publication_complete_papers'],38)
        self.assertNotEqual(first,second)
        self.assertNotIn(KEY,(first/'comparison.json').read_text())
        self.assertIn('not a 20000-ID prediction',result['conclusion'])

    def test_partial_or_different_data_signatures_cannot_rank_speed(self):
        audits={'A':[evidence('combined')],'B':[evidence('arxiv'),evidence('s2')]}
        with patch.object(self.session,'_audits',side_effect=lambda arm:audits[arm]),patch.object(
                self.session,'_integrity',side_effect=lambda arm,**kw:dict(ready=True,signature=arm)):
            result,_=self.session.compare()
        self.assertFalse(result['speed_comparison_eligible'])

    def test_notebook_compiles_and_buttons_are_not_called_on_setup(self):
        nb=json.loads((ROOT/'notebooks/m1_colab_strategy_comparison.ipynb').read_text(encoding='utf-8'))
        for c in nb['cells']:
            if c['cell_type']=='code':
                tree=ast.parse(''.join(c['source']))
                self.assertEqual(c['outputs'],[]);self.assertIsNone(c['execution_count'])
                self.assertFalse(any(isinstance(n,ast.Attribute) and isinstance(n.value,ast.Name)
                                     and n.value.id=='session' and n.attr in {'preflight','run'}
                                     for n in ast.walk(tree)))


class Widget:
    def __init__(self,*args,**kwargs):
        self.value=False;self.disabled=False;self.callbacks=[]
    def on_click(self,callback):self.callbacks.append(callback)
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def click(self):
        for callback in self.callbacks:callback(self)


class ControlsTests(unittest.TestCase):
    def test_buttons_require_checkbox_and_bind_correct_actions_without_auto_network(self):
        widgets=Mock(Checkbox=Widget,Output=Widget,Button=Widget,Layout=Widget,VBox=Widget,HBox=Widget)
        session=Mock();session.run.return_value=[]
        controls=cc.build_controls(session,widgets,Mock(),Mock())
        session.run.assert_not_called();session.preflight.assert_not_called()
        with patch('sys.stdout',new_callable=io.StringIO):
            controls['buttons']['A'].click()
            session.run.assert_not_called()
            controls['cpu'].value=controls['writer'].value=True
            controls['buttons']['A'].click()
        session.run.assert_called_once_with('A',resume=False,reviewed=False)
        self.assertFalse(any(b.disabled for b in controls['buttons'].values()))

    def test_callback_exception_does_not_print_secret_and_restores_controls(self):
        widgets=Mock(Checkbox=Widget,Output=Widget,Button=Widget,Layout=Widget,VBox=Widget,HBox=Widget)
        session=Mock();session.run.side_effect=RuntimeError(KEY)
        controls=cc.build_controls(session,widgets,Mock(),Mock())
        controls['cpu'].value=controls['writer'].value=True
        with patch('sys.stdout',new_callable=io.StringIO) as output:
            controls['buttons']['resume_B'].click()
        self.assertNotIn(KEY,output.getvalue())
        session.run.assert_called_once_with('B',resume=True,reviewed=False)
        self.assertFalse(any(b.disabled for b in controls['buttons'].values()))


if __name__=='__main__':unittest.main()
