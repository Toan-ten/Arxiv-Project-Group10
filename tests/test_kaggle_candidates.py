"""Offline qualification of snapshot candidates and arXiv confirmation."""
import json
from pathlib import Path
from unittest.mock import patch

from src.kaggle_candidates import build_candidate_index, prepare_snapshot
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
