"""Offline acceptance for automatic batches and immutable CSV/JSON publication."""
import builtins
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import m2_colab_benchmark as bench
from scripts import m2_colab_export as export
from src.m2 import runner
from tests.test_m2_colab_benchmark import setup, offline, files_hashes, read_csv, REPO
from tests.test_m2_colab_export import fixture_archive


@pytest.mark.parametrize("start,end,size,want", [
    (1,200,50,[(1,50),(51,100),(101,150),(151,200)]),
    (101,150,50,[(101,150)]), (103,154,50,[(103,152),(153,154)]),
    (1,1,50,[(1,1)]), (101,105,2,[(101,102),(103,104),(105,105)])])
def test_boundaries(start,end,size,want):
    groups = bench.batches(start,end,size)
    assert [(g[0],g[-1]) for g in groups] == want
    assert [n for g in groups for n in g] == list(range(start,end+1))


@pytest.mark.parametrize("size", [0,201,True,1.5,"50"])
def test_invalid_batch_size(size):
    with pytest.raises(ValueError):
        bench.batches(1,200,size)


def three_cohorts(setup):
    config, archives, _ = setup
    for number in (102,103):
        fixture_archive(archives.parent, number=number)
    return config | dict(end_cohort=103,cohorts_per_batch=2,auto_continue=True)


def test_auto_sequence_publishes_before_next_parse_and_aggregate_not_sum_rates(setup,monkeypatch):
    config = three_cohorts(setup)
    _,root,_ = bench.paths(config)
    original = runner.run
    calls=[]
    def observed(corpus,output):
        calls.append(Path(output).name)
        if len(calls)==3:
            pointer=export.read(root/'reports/batch_0101-0102/LATEST.json')
            assert bench.verify_snapshot(Path(pointer['report_directory']))['status']=='complete'
        return original(corpus,output)
    monkeypatch.setattr(runner,'run',observed)
    result=bench.operate(config,'run')
    assert len(calls)==3 and result['summary']['versions']==12
    assert result['summary']['successful_versions']==3
    assert result['summary']['version_success_rate_percent']==25
    assert result['summary']['cohorts_completed_count']==3
    first=Path(export.read(root/'reports/batch_0101-0102/LATEST.json')['report_directory'])
    second=Path(export.read(root/'reports/batch_0103-0103/LATEST.json')['report_directory'])
    a=export.read(first/'benchmark_summary.json'); b=export.read(second/'benchmark_summary.json')
    assert a['versions']==8 and b['versions']==4
    assert {v['cohort'] for v in read_csv(first/'paper_results.csv')}=={'cohort_0101_corpus','cohort_0102_corpus'}
    assert a['measured_active_seconds']+b['measured_active_seconds']==pytest.approx(result['summary']['measured_active_seconds'])
    before=files_hashes(first)
    monkeypatch.setattr(runner,'run',Mock(side_effect=AssertionError('No parser during report or resume')))
    bench.operate(config,'report'); bench.operate(config,'run')
    assert files_hashes(first)==before
    assert not list(root.rglob('*.png')) and not list(root.rglob('*.md'))


def test_manual_continue_and_resize_preserve_campaign_and_completed(setup,monkeypatch):
    config=three_cohorts(setup) | dict(cohorts_per_batch=1,auto_continue=False)
    first=bench.operate(config,'run')
    assert first['summary']['cohorts_completed_count']==1 and not first['summary']['benchmark_complete']
    _,root,_=bench.paths(config)
    old=files_hashes(root/'native/cohort_0101_corpus')
    changed=config | dict(cohorts_per_batch=50,auto_continue=True)
    assert bench.paths(changed)[1]==root
    original=runner.run
    def no_duplicate(corpus,output):
        assert Path(output).name!='cohort_0101_corpus'
        return original(corpus,output)
    monkeypatch.setattr(runner,'run',no_duplicate)
    second=bench.operate(changed,'run')
    assert second['summary']['benchmark_complete']
    assert files_hashes(root/'native/cohort_0101_corpus')==old


def test_mid_batch_stop_partial_then_restart(setup,monkeypatch):
    config=three_cohorts(setup)
    original=runner.run
    calls=[]
    def interrupt(corpus,output):
        calls.append(Path(output).name)
        if len(calls)==2:
            raise KeyboardInterrupt('fixture stop')
        return original(corpus,output)
    monkeypatch.setattr(runner,'run',interrupt)
    with pytest.raises(bench.BenchmarkFailure):
        bench.operate(config,'run')
    _,root,_=bench.paths(config)
    partial=Path(export.read(root/'reports/batch_0101-0102/LATEST.json')['report_directory'])
    assert bench.verify_snapshot(partial)['status']=='partial'
    assert export.read(partial/'benchmark_summary.json')['cohorts_completed_count']==1
    assert not (root/'native/cohort_0103_corpus').exists()
    old=files_hashes(partial)
    resumed=[]
    def resume(corpus,output):
        resumed.append(Path(output).name)
        return original(corpus,output)
    monkeypatch.setattr(runner,'run',resume)
    result=bench.operate(config,'run')
    assert result['summary']['benchmark_complete']
    assert resumed==['cohort_0102_corpus','cohort_0103_corpus']
    assert files_hashes(partial)==old


def test_report_publication_interruption_has_no_ready_pointer(setup,monkeypatch):
    config,_,_=setup
    original=export.immutable
    def fail(path,data):
        if Path(path).name=='report_checksums.json':
            raise OSError('fixture report write interrupted')
        return original(path,data)
    monkeypatch.setattr(export,'immutable',fail)
    with pytest.raises(OSError,match='interrupted'):
        bench.operate(config,'smoke')
    _,root,_=bench.paths(config)
    assert (root/'completed/cohort_0101_corpus.json').is_file()
    assert not (root/'LATEST.json').exists()
    assert not list((root/'reports').rglob('READY.json'))
    monkeypatch.setattr(export,'immutable',original)
    monkeypatch.setattr(runner,'run',Mock(side_effect=AssertionError('Recover report without parse')))
    result=bench.operate(config,'run')
    assert bench.verify_snapshot(Path(result['report_directory']))['status']=='complete'


def test_report_checksum_tamper_stops_without_overwrite(setup):
    config,_,_=setup
    result=bench.operate(config,'smoke')
    path=Path(result['report_directory'])/'paper_results.csv'
    path.write_bytes(b'corrupt report')
    with pytest.raises(bench.BenchmarkFailure,match='checksum mismatch'):
        bench.operate(config,'report')
    assert path.read_bytes()==b'corrupt report'


def test_raw_latex_never_imports_plotting_or_renders_math(setup,monkeypatch):
    config,_,name=setup
    original=builtins.__import__
    def deny(name,*a,**kw):
        if name.startswith('matplotlib'):
            raise AssertionError('Plotting forbidden')
        return original(name,*a,**kw)
    monkeypatch.setattr(builtins,'__import__',deny)
    result=bench.operate(config,'smoke')
    _,root,_=bench.paths(config)
    event=dict(cohort=name,event_id='rawlatex',started_at=bench.now(),elapsed_seconds=0,
        record=dict(cohort=name,versions=[],audit_outcome='failed',errors=[dict(cohort=name,arxiv_id=None,version=None,
          error_category='unknown',error_type=r'$SU(6)$ \\repr{\\bar6}',error_message=r'$SU(6)$ \\repr{\\bar6}',
          source_file=None,exception_class=None,traceback_path=None,status='stopped',parser_sha=config['release_commit'])]))
    export.immutable(root/'events/rawlatex/started.json',export.encoded(event))
    report=bench.operate(config,'report')
    assert read_csv(Path(report['report_directory'])/'m2_errors.csv')[0]['error_message']==event['record']['errors'][0]['error_message']
    assert not list(root.rglob('*.png'))


def test_shared_member_folders_and_preflight_all_selected_before_parse(setup,monkeypatch):
    import shutil
    config,archives,name=setup
    folder=archives/'member-3/archives'; folder.mkdir(parents=True)
    for p in list(archives.iterdir()):
        if p.is_file(): shutil.move(str(p),str(folder/p.name))
    config['end_cohort']=102
    plan=bench.operate(config,'plan')
    assert [r['available'] for r in plan['input_inventory']]==[True,False]
    monkeypatch.setattr(runner,'run',Mock(side_effect=AssertionError('Missing selected input must stop preflight')))
    with pytest.raises(bench.BenchmarkFailure,match='cohort_0102_corpus.tar.gz'):
        bench.operate(config,'run')


def test_legacy_release_checkpoint_compatibility_preserves_provenance_and_bytes(setup,monkeypatch,tmp_path):
    config,_,_=setup
    oldsha='f5562be0477ed844121bdd429b3c974dfd9cd41c'
    blob=bench.subprocess.check_output(['git','-C',str(REPO),'show',oldsha+':scripts/m2_colab_benchmark.py'])
    modulefile=tmp_path/'legacy_benchmark.py'; modulefile.write_bytes(blob)
    spec=importlib.util.spec_from_file_location('legacy_benchmark',modulefile)
    legacy=importlib.util.module_from_spec(spec); spec.loader.exec_module(legacy)
    original=bench.subprocess.check_output
    with monkeypatch.context() as patch:
        patch.setattr(legacy,'verify_runtime',lambda c:dict(parser_sha=oldsha,parser_source_commit=bench.PARSER_SOURCE_COMMIT,parser_fingerprint=bench.PARSER_FINGERPRINT))
        patch.setattr(legacy,'charts',lambda *a:None)
        def legacy_head(args,*a,**kw):
            if args[-2:]==['rev-parse','HEAD']:return oldsha+'\n'
            return original(args,*a,**kw)
        patch.setattr(bench.subprocess,'check_output',legacy_head)
        oldconfig=config | dict(release_commit=oldsha)
        legacy.operate(oldconfig,'smoke')
        _,root,_=legacy.paths(oldconfig)
    preserved={n:files_hashes(root/n) for n in ('native','completed','native_evidence','events')}
    identitybytes=(root/'benchmark.json').read_bytes()
    newconfig=config | dict(benchmark_campaign=str(root),cohorts_per_batch=50)
    assert bench.paths(newconfig)[1]==root
    monkeypatch.setattr(runner,'run',Mock(side_effect=AssertionError('Legacy verified cohort must not parse')))
    result=bench.operate(newconfig,'run')
    assert result['summary']['parser_shas']==[oldsha]
    assert result['summary']['provenance']['parser_sha']==config['release_commit']
    assert (root/'benchmark.json').read_bytes()==identitybytes
    assert all(files_hashes(root/n)==hashes for n,hashes in preserved.items())
    with pytest.raises(ValueError,match='scope/input/parser/timeout'):
        bench.paths(newconfig | dict(start_cohort=1,end_cohort=200))
    source=export.read(root/'benchmark.json')
    source['identity']['helper_sha256']='f'*64
    # A forged identity must be rejected, never repaired or overwritten.
    (root/'benchmark.json').write_bytes(export.encoded(source))
    with pytest.raises(ValueError,match='identity hash'):
        bench.paths(newconfig)


def test_aggregate_recomputes_weighted_rates_and_keeps_unaudited_separate():
    def record(name,states,audit='verified'):
        return dict(cohort=name,audit_outcome=audit,errors=[],versions=[dict(cohort=name,
            arxiv_id=f'paper-{i}',version=1,status=state,parser_sha='a'*40) for i,state in enumerate(states)])
    a=record('cohort_0101_corpus',['success'])
    b=record('cohort_0102_corpus',['parse_error']*3)
    result=bench.summary([a,b],[],[a['cohort'],b['cohort']],Path('benchmark'),{})
    assert result['version_success_rate_percent']==25
    assert result['paper_success_rate_percent']==25
    assert result['rate_denominators']['version_rates']==4
    c=record('cohort_0103_corpus',['success']*9,'failed')
    result=bench.summary([a,b,c],[],[a['cohort'],b['cohort'],c['cohort']],Path('benchmark'),{})
    assert result['versions']==13 and result['verified_versions']==4
    assert result['version_success_rate_percent']==25 and not result['benchmark_complete']


def test_completed_cohort_reaudits_after_missing_input_preflight_without_reparse(setup,monkeypatch):
    config,archives,name=setup
    bench.operate(config,'smoke')
    path=archives/(name+'.tar.gz.sha256'); data=path.read_bytes(); path.unlink()
    with pytest.raises(bench.BenchmarkFailure):
        bench.operate(config,'run')
    path.write_bytes(data)
    monkeypatch.setattr(runner,'run',Mock(side_effect=AssertionError('Still cached')))
    result=bench.operate(config,'run')
    assert result['summary']['benchmark_complete'] and result['summary']['audit_errors']==0


def test_ready_marker_cannot_claim_complete_for_partial_data(setup):
    config,_,_=setup
    result=bench.operate(config | dict(end_cohort=150),'smoke')
    path=Path(result['report_directory'])
    ready=export.read(path/'READY.json'); ready['status']='complete'
    (path/'READY.json').write_bytes(export.encoded(ready))
    with pytest.raises(bench.BenchmarkFailure,match='scope/status'):
        bench.verify_snapshot(path)
