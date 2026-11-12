"""Offline qualification of snapshot candidates and arXiv confirmation."""
import json
import hashlib
from pathlib import Path
import pytest
from unittest.mock import patch

from src.kaggle_candidates import (
    build_candidate_index,
    compare_candidate_to_discovery,
    file_sha256,
    prepare_snapshot,
    persist_discrepancy_evidence,
    validate_discrepancy_checkpoint,
)
from test_pipeline_history import HistoryIntegrationTests, PAIR
import test_pipeline as fx


def record(identifier, dates=('2025-02-12', '2025-02-12')):
    return {'id': identifier, 'authors': 'Nguyen, An and Smith, Jo',
            'authors_parsed': [['Nguyen', 'An', ''], ['Smith', 'Jo', '']],
            'versions': [{'version': f'v{i}', 'created': d} for i, d in enumerate(dates, 1)]}


def test_index_refuses_duplicates_invalid_versions_and_bad_lines(tmp_path):
    path = tmp_path / 'snapshot.json'
    rows = [record('2501.00001'), record('2501.00002'), record('2501.00002'),
            {'id':'2501.00003','versions':[{'version':'v2','created':'2025-01-01'}]}]
    path.write_bytes(('\n'.join(json.dumps(r) for r in rows)+'\n{bad\n').encode()+b'\xff\n')
    output=tmp_path/'index.json'
    ids=[('1', f'2501.0000{i}') for i in range(1, 5)]
    index=build_candidate_index(path, ids, output)
    assert list(index['candidates']) == ['2501.00001']
    assert index['candidates']['2501.00001']['authors'] == ['An Nguyen', 'Jo Smith']
    assert index['diagnostics'] == {'duplicate_ids':['2501.00002'], 'invalid_ids':['2501.00003'],
                                    'missing_ids':['2501.00004'], 'malformed_lines':2}
    assert json.loads(output.read_text())['snapshot_sha256'] == index['snapshot_sha256']


def test_snapshot_index_records_download_and_provider_update_provenance(tmp_path):
    snapshot = tmp_path / 'arxiv-metadata-oai-snapshot.json'
    snapshot.write_text(json.dumps(record('2501.00001')) + '\n')
    (tmp_path / 'snapshot.manifest.json').write_text(json.dumps({
        'sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        'downloaded_at': '2026-11-12T01:02:03+00:00',
        'provider_updated_at': None,
    }))
    result = build_candidate_index(snapshot, [('1', '2501.00001')], tmp_path / 'index.json')
    assert result['schema_version'] == 2
    assert result['downloaded_at'] == '2026-11-12T01:02:03+00:00'
    assert result['provider_updated_at'] is None
    assert result['comparison_precision'] == 'calendar_day_utc'


def test_real_kaggle_rfc2822_version_dates_are_normalized_to_utc_days(tmp_path):
    identifier = '2501.00001'
    snapshot = tmp_path / 'arxiv-metadata-oai-snapshot.json'
    kaggle_record = record(identifier, (
        'Mon, 2 Apr 2007 19:18:42 GMT',
        'Tue, 3 Apr 2007 00:30:00 +0500',
    ))
    snapshot.write_text(json.dumps(kaggle_record) + '\n', encoding='utf-8')

    index = build_candidate_index(snapshot, [('1', identifier)], tmp_path / 'index.json')

    assert index['candidates'][identifier]['dates'] == ['2007-04-02', '2007-04-02']
    assert index['diagnostics']['invalid_ids'] == []
    assert index['diagnostics']['missing_ids'] == []


def test_rfc_date_parser_version_does_not_reuse_legacy_invalid_index(tmp_path):
    from src.kaggle_candidates import reuse_or_build_index

    identifier = '2501.00001'
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps(record(identifier, (
        'Mon, 2 Apr 2007 19:18:42 GMT',
    ))) + '\n', encoding='utf-8')
    checksum = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    ids = [identifier]
    legacy_key = hashlib.sha256(json.dumps(
        [2, checksum, ids], separators=(',', ':')
    ).encode()).hexdigest()
    legacy_path = tmp_path / 'indices' / f'candidates_{legacy_key}.json'
    legacy_path.parent.mkdir(parents=True)
    legacy = build_candidate_index(snapshot, [('1', identifier)], legacy_path)
    legacy['candidates'] = {}
    legacy['diagnostics']['invalid_ids'] = [identifier]
    legacy_path.write_text(json.dumps(legacy), encoding='utf-8')

    current = reuse_or_build_index(snapshot, [('1', identifier)], legacy_path.parent, checksum)

    assert current['reused'] is False
    assert current['index_path'] != str(legacy_path)
    assert current['index']['candidates'][identifier]['dates'] == ['2007-04-02']
    assert json.loads(legacy_path.read_text(encoding='utf-8'))['candidates'] == {}


def test_comparison_uses_exact_versions_and_utc_calendar_days():
    identifier = '2501.00001'
    candidate = {'version_ids': [identifier + 'v1', identifier + 'v2'],
                 'dates': ['2025-02-12', '2025-02-12']}
    discovery = {
        'arxiv_id': identifier, 'status': 'found', 'http_status': 200,
        'version_status': 'resolved',
        'version_ids': [identifier + 'v1', identifier + 'v2'],
        'published': '2025-02-12T00:30:00-02:00',
        'updated': '2025-02-12T02:00:00Z',
    }
    assert compare_candidate_to_discovery(candidate, discovery)['status'] == 'match'
    discovery['updated'] = '2025-02-13T00:00:00Z'
    result = compare_candidate_to_discovery(candidate, discovery)
    assert result['status'] == 'discrepancy'
    assert 'latest_date' in result['mismatches']
    discovery['version_ids'] = [identifier + 'v1']
    assert 'version_ids' in compare_candidate_to_discovery(candidate, discovery)['mismatches']
    assert compare_candidate_to_discovery(None, discovery)['status'] == 'unusable'


def test_discrepancy_evidence_is_immutable_and_checkpointed(tmp_path):
    evidence = {'schema_version': 1, 'session_id': 'session-a',
                'arxiv_id': '2501.00001', 'decision': 'use_arxiv_latest'}
    pointer = persist_discrepancy_evidence(
        evidence, evidence_root=tmp_path / 'evidence',
        checkpoint_root=tmp_path / 'checkpoints', session_id='session-a',
    )
    Path(pointer['checkpoint_path']).unlink()
    from src.kaggle_candidates import validate_discrepancy_checkpoints
    recovered = validate_discrepancy_checkpoints(tmp_path / 'checkpoints', tmp_path / 'evidence')
    assert recovered[0]['recovered_from_orphan'] is True
    checked = validate_discrepancy_checkpoint(pointer['checkpoint_path'])
    assert checked['sha256'] == pointer['sha256']
    with pytest.raises(FileExistsError):
        persist_discrepancy_evidence(
            {**evidence, 'decision': 'different'}, evidence_root=tmp_path / 'evidence',
            checkpoint_root=tmp_path / 'checkpoints', session_id='session-a',
        )
    sidecar = Path(pointer['artifact_path']).with_suffix('.json.sha256')
    sidecar.write_text('0' * 64)
    with pytest.raises(ValueError, match='checksum'):
        validate_discrepancy_checkpoint(pointer['checkpoint_path'])


def test_missing_candidate_discrepancy_is_auditable_from_index_diagnostics(tmp_path):
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps(record('2501.00002')) + '\n', encoding='utf-8')
    index_path = tmp_path / 'index.json'
    index = build_candidate_index(snapshot, [('1', '2501.00001')], index_path)
    evidence = {
        'schema_version': 1, 'session_id': 'session-missing',
        'arxiv_id': '2501.00001', 'mssv': '1', 'observed_at': '2026-11-12T00:00:00Z',
        'kaggle': {
            'snapshot_path': str(snapshot), 'index_path': str(index_path),
            'snapshot_sha256': index['snapshot_sha256'],
            'index_sha256': file_sha256(index_path),
            'version_ids': None, 'dates': None, 'diagnostic': 'missing',
        },
    }
    pointer = persist_discrepancy_evidence(
        evidence, evidence_root=tmp_path / 'evidence',
        checkpoint_root=tmp_path / 'checkpoints', session_id='session-missing',
    )
    assert validate_discrepancy_checkpoint(pointer['checkpoint_path'])['arxiv_id'] == '2501.00001'


def test_cache_rejects_mismatched_checksum_without_overwrite(tmp_path):
    target=tmp_path/'arxiv-metadata-oai-snapshot.json'
    target.write_text('original')
    (tmp_path/'snapshot.manifest.json').write_text(json.dumps({'sha256':'0'*64}))
    result=prepare_snapshot(tmp_path, token='secret')
    assert result['reason']=='invalid_existing_cache'
    assert target.read_text()=='original'
    assert 'secret' not in str(result)


def test_missing_kaggle_token_falls_back(tmp_path):
    assert prepare_snapshot(tmp_path)['reason']=='missing_kaggle_token'


class TestCandidatesPipeline(HistoryIntegrationTests):
    def test_legacy_kaggle_history_is_readable_but_not_trusted_by_new_m1_policy(self):
        from src import arxiv_history
        self.success(n=2, history=False)
        self.run_pipeline(revision_history=False)
        path = self.paper_path() / 'metadata.json'
        metadata = json.loads(path.read_text(encoding='utf-8'))
        metadata['versions'][1].update(date='2025-02-13', date_status='verified')
        metadata['revised_dates'] = ['2025-02-13']
        metadata['provenance']['version_history'] = {
            'provider': 'kaggle_snapshot', 'query_identifier': PAIR[1],
            'retrieved_at': '2026-11-12T00:00:00+00:00',
            'snapshot': ['2025-02-13'], 'snapshot_sha256': 'a' * 64,
            'dataset': 'Cornell-University/arxiv', 'snapshot_time': 'unknown',
        }
        path.write_text(json.dumps(metadata), encoding='utf-8')
        loaded = self.store().read_metadata(*PAIR)
        self.assertTrue(arxiv_history._cached(loaded))
        with patch.dict('os.environ', {'M1_KAGGLE_POLICY_VERSION': 'm1-kaggle-reconcile-v2'}):
            self.assertFalse(arxiv_history._cached(loaded))

    def test_fresh_arxiv_matching_kaggle_candidate_supplies_history(self):
        self.success(n=2, history=False)
        index={'status':'complete','snapshot_sha256':'a'*64, 'snapshot_time':'unknown',
               'dataset':'Cornell-University/arxiv',
               'candidates':{PAIR[1]:{'version_ids':[PAIR[1]+'v1',PAIR[1]+'v2'],
                                        'dates':['2025-02-12','2025-02-12']}}}
        path=self.root.parent/'index.json'; path.write_text(json.dumps(index))
        with patch.dict('os.environ', {'M1_KAGGLE_CANDIDATE_INDEX':str(path)}):
            paper=self.run_pipeline()['papers'][0]
        self.assertEqual(paper['revision_history']['provider'],'kaggle_snapshot')
        self.assertEqual(paper['revision_history']['status'],'complete')
        self.assertEqual(len(self.requests),3)
        self.assertEqual(self.store().read_metadata(*PAIR)['revised_dates'], ['2025-02-12'])

    def test_withdrawn_source_404_uses_matching_kaggle_dates_after_page_verification(self):
        index={'status':'complete','snapshot_sha256':'a'*64, 'snapshot_time':'unknown',
               'dataset':'Cornell-University/arxiv',
               'candidates':{PAIR[1]:{'version_ids':[PAIR[1]+'v1',PAIR[1]+'v2'],
                                        'dates':['2025-02-12','2025-02-12']}}}
        path=self.root.parent/'index-withdrawn.json'; path.write_text(json.dumps(index))
        self.outcomes.extend([
            fx.xml_response(PAIR[1], 2), fx.Response(fx.TEX), fx.http_error(404),
            fx.Response(
                (f'<html>arXiv:{PAIR[1]}v2 This paper has been withdrawn by the author.'
                 f'</html>').encode(), 'text/html'),
        ])
        with patch.dict('os.environ', {'M1_KAGGLE_CANDIDATE_INDEX':str(path)}):
            paper=self.run_pipeline()['papers'][0]
        meta=self.store().read_metadata(*PAIR)
        self.assertEqual(paper['source']['v2']['status'], 'withdrawn_version')
        self.assertEqual(paper['revision_history']['provider'], 'kaggle_snapshot')
        self.assertEqual(paper['revision_history']['status'], 'complete')
        self.assertEqual(meta['revised_dates'], ['2025-02-12'])

    def test_resume_rechecks_saved_404_and_completes_history_from_matching_candidate(self):
        index={'status':'complete','snapshot_sha256':'b'*64, 'snapshot_time':'unknown',
               'dataset':'Cornell-University/arxiv',
               'candidates':{PAIR[1]:{'version_ids':[PAIR[1]+'v1',PAIR[1]+'v2'],
                                        'dates':['2025-02-12','2025-02-12']}}}
        path=self.root.parent/'index-resume.json'; path.write_text(json.dumps(index))
        self.outcomes.extend([
            fx.xml_response(PAIR[1], 2), fx.Response(fx.TEX), fx.http_error(404),
            fx.Response(f'<html>arXiv:{PAIR[1]}v2</html>'.encode(), 'text/html'),
        ])
        with patch.dict('os.environ', {'M1_KAGGLE_CANDIDATE_INDEX':str(path)}):
            first=self.run_pipeline()['papers'][0]
        self.assertEqual(first['source']['v2']['status'], 'http_error')
        metadata_path = self.paper_path() / 'metadata.json'
        metadata = json.loads(metadata_path.read_text())
        metadata['provenance']['arxiv']['retrieved_at'] = '2020-01-01T00:00:00+00:00'
        metadata_path.write_text(json.dumps(metadata))

        self.outcomes.extend([
            fx.xml_response(PAIR[1], 2),
            fx.Response(
                (f'<html>arXiv:{PAIR[1]}v2 This paper has been withdrawn by the author.'
                 f'</html>').encode(), 'text/html'),
        ])
        with patch.dict('os.environ', {'M1_KAGGLE_CANDIDATE_INDEX':str(path)}):
            resumed=self.run_pipeline()['papers'][0]
        self.assertEqual(resumed['source']['v2']['status'], 'withdrawn_version')
        self.assertEqual(resumed['revision_history']['provider'], 'kaggle_snapshot')
        self.assertEqual(resumed['revision_history']['status'], 'complete')

    def test_stale_candidate_cannot_skip_version_queries(self):
        self.success(n=3)
        index={'status':'complete','snapshot_sha256':'a'*64, 'snapshot_time':'unknown',
               'dataset':'Cornell-University/arxiv',
               'candidates':{PAIR[1]:{'version_ids':[PAIR[1]+'v1',PAIR[1]+'v2'],
                                        'dates':['2025-02-12','2025-02-12']}}}
        path=self.root.parent/'index.json'; path.write_text(json.dumps(index))
        with patch.dict('os.environ', {'M1_KAGGLE_CANDIDATE_INDEX':str(path)}):
            paper=self.run_pipeline()['papers'][0]
        self.assertEqual(paper['revision_history']['provider'], 'arxiv_atom_versioned')
        self.assertEqual(len(self.requests), 7)


def test_snapshot_download_and_reuse_offline(tmp_path, capsys):
    import subprocess
    import zipfile

    class FakeProcess:
        def __init__(self, args, **kwargs):
            target = Path(args[args.index('-p') + 1])
            filename = 'arxiv-metadata-oai-snapshot.json'
            with zipfile.ZipFile(target / (filename + '.zip'), 'w') as archive:
                archive.writestr(filename, json.dumps(record('2501.00001')) + '\n')
            assert kwargs['env']['KAGGLE_API_TOKEN'] == 'private-token'
            self.returncode = None
            self.wait_calls = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired('kaggle', timeout)
            self.returncode = 0
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    with patch('subprocess.Popen', FakeProcess):
        first=prepare_snapshot(tmp_path, token='private-token')
    output = capsys.readouterr().out
    assert first['status']=='ready' and first['source']=='downloaded'
    assert 'bắt đầu tải snapshot' in output
    assert 'vẫn đang tải snapshot' in output
    assert 'đang kiểm tra snapshot và tính SHA-256' in output
    assert 'private-token' not in output
    assert 'private-token' not in json.dumps(first)
    assert 'private-token' not in (tmp_path/'snapshot.manifest.json').read_text()
    second=prepare_snapshot(tmp_path)
    assert second['source']=='cached' and second['snapshot_sha256']==first['snapshot_sha256']


def test_manual_stop_terminates_kaggle_process_and_removes_temporary_files(tmp_path, capsys):
    import pytest

    class InterruptedProcess:
        def __init__(self, *_args, **_kwargs):
            self.returncode = None
            self.wait_calls = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise KeyboardInterrupt
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    with patch('subprocess.Popen', InterruptedProcess):
        with pytest.raises(KeyboardInterrupt):
            prepare_snapshot(tmp_path, token='private-token')

    output = capsys.readouterr().out
    assert 'đã dừng tiến trình tải' in output
    assert 'private-token' not in output
    assert not list(tmp_path.glob('.kaggle-*'))
    assert not (tmp_path / 'arxiv-metadata-oai-snapshot.json').exists()


def test_index_reuse_requires_matching_snapshot_and_scope(tmp_path):
    from src.kaggle_candidates import file_sha256, reuse_or_build_index
    import pytest
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps(record('2501.00001'))+'\n')
    checksum = file_sha256(snapshot)
    one = reuse_or_build_index(snapshot, [('1', '2501.00001')], tmp_path/'indices', checksum)
    again = reuse_or_build_index(snapshot, [('1', '2501.00001')], tmp_path/'indices', checksum)
    other = reuse_or_build_index(snapshot, [('1', '2501.00002')], tmp_path/'indices', checksum)
    assert not one['reused'] and again['reused'] and not other['reused']
    snapshot.write_text('changed')
    with pytest.raises(ValueError, match='checksum'):
        reuse_or_build_index(snapshot, [('1', '2501.00001')], tmp_path/'indices', checksum)
