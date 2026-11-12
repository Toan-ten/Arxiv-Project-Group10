"""Refresh cached discovery without overwriting completed corpus evidence."""
from unittest.mock import patch
import json
import test_pipeline as fx
from test_s2_integration import S2IntegrationTests, PAIR
from src import arxiv_discovery, pipeline


class TestCachedRefresh(S2IntegrationTests):
    def _age_cached_discovery(self):
        path = self.paper_path() / "metadata.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["provenance"]["arxiv"]["retrieved_at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_fresh_cached_discovery_resumes_without_arxiv_request(self):
        self.arxiv_success(); self.s2_success(); self.run_on()
        self.outcomes = []
        report = self.run_on(revision_history=True, versions=None)
        self.assertFalse(report["stopped"])
        self.assertEqual(report["papers"][0]["discovery"]["action"], "cached")

    def test_same_version_refresh_persists_new_arxiv_snapshot_and_preserves_stages(self):
        self.arxiv_success(); self.s2_success(); self.run_on()
        store=self.store()
        before_source=(self.paper_path()/'v1/main.tex').read_bytes()
        before_refs=store.read_references(*PAIR)
        previous=store.read_metadata(*PAIR)['provenance']['arxiv']['snapshot']
        fresh=arxiv_discovery.parse_arxiv_response(fx.atom(versions=1), *PAIR)
        fresh.update(http_status=200, attempts=1, comments='freshly verified comment')
        store.refresh_discovery(fresh)
        after=store.read_metadata(*PAIR)
        self.assertEqual(after['provenance']['arxiv']['snapshot']['comments'], 'freshly verified comment')
        self.assertEqual(after['provenance']['arxiv']['previous_discovery']['snapshot'], previous)
        self.assertEqual((self.paper_path()/'v1/main.tex').read_bytes(), before_source)
        self.assertEqual(store.read_references(*PAIR), before_refs)

    def test_comparison_fingerprint_requires_complete_integrity(self):
        from src.session_evidence import fingerprint_complete_corpus
        self.arxiv_success(); self.s2_success(); self.run_on()
        digest = fingerprint_complete_corpus(self.store(), [PAIR])
        self.assertEqual(len(digest), 64)
        (self.paper_path()/'v1/main.tex').write_text('tampered')
        self.assertIsNone(fingerprint_complete_corpus(self.store(), [PAIR]))

    def test_complete_paper_new_version_preserves_content(self):
        self.arxiv_success(); self.s2_success(); self.run_on()
        store=self.store(); before_refs=store.read_references(*PAIR)
        before_source=(self.paper_path()/'v1/main.tex').read_bytes()
        before_meta=store.read_metadata(*PAIR)
        fresh=arxiv_discovery.parse_arxiv_response(fx.atom(versions=2), *PAIR)
        fresh.update(http_status=200, attempts=1)
        result=store.refresh_discovery(fresh)
        after=store.read_metadata(*PAIR)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(after['status'], 'partial')
        self.assertEqual(after['versions'][-1]['version'], 'v2')
        self.assertIsNone(after['revised_dates'])
        self.assertEqual(after['provenance']['semantic_scholar'],before_meta['provenance']['semantic_scholar'])
        self.assertEqual(store.read_references(*PAIR),before_refs)
        self.assertEqual((self.paper_path()/'v1/main.tex').read_bytes(),before_source)
        self.assertEqual(store.read_manifest(*PAIR)['versions']['v2']['status'],'pending')

    def test_resume_downloads_new_version_and_recompletes(self):
        from test_arxiv_history import entry
        self.arxiv_success(); self.s2_success(); self.run_on()
        self._age_cached_discovery()
        original=(self.paper_path()/'v1/main.tex').read_bytes()
        self.outcomes=[fx.xml_response(versions=2), fx.Response(fx.TEX),
                       fx.Response(entry('v1'), 'application/atom+xml'),
                       fx.Response(entry('v2'), 'application/atom+xml')]
        report=self.run_on(revision_history=True, versions=None)
        paper=report['papers'][0]
        self.assertEqual(paper['paper_status'], 'complete')
        self.assertEqual(self.store().read_manifest(*PAIR)['versions']['v2']['status'], 'success')
        self.assertEqual((self.paper_path()/'v1/main.tex').read_bytes(),original)
        self.assertEqual(len(self.store().read_metadata(*PAIR)['revised_dates']),1)

    def test_interrupted_manifest_commit_recovers_pending_version(self):
        self.arxiv_success(); self.s2_success(); self.run_on()
        store=self.store()
        fresh=arxiv_discovery.parse_arxiv_response(fx.atom(versions=2), *PAIR)
        fresh.update(http_status=200, attempts=1)
        with patch.object(store, '_commit', side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError):
                store.refresh_discovery(fresh)
        plan=store.resume(*PAIR)
        self.assertIn('v2',plan['retry'])
        self.assertEqual(plan['manifest']['status'],'partial')
        self.assertEqual(store.read_references(*PAIR)['status'],'complete')

    def test_refresh_refuses_submission_date_conflict(self):
        from src.storage import StorageError
        self.arxiv_success(); self.s2_success(); self.run_on()
        before=self.snapshot()
        fresh=arxiv_discovery.parse_arxiv_response(fx.atom(versions=2), *PAIR)
        fresh.update(http_status=200, attempts=1, published='2025-03-15T00:00:00Z')
        with self.assertRaises(StorageError):
            self.store().refresh_discovery(fresh)
        self.assertEqual(self.snapshot(),before)

    def test_refuses_lost_or_mismatched_versions(self):
        from src.storage import StorageError
        self.arxiv_success(); self.s2_success(); self.run_on()
        store=self.store(); before=self.snapshot()
        fresh=arxiv_discovery.parse_arxiv_response(fx.atom(versions=2), *PAIR)
        fresh.update(http_status=200, attempts=1)
        fresh['version_ids']=[PAIR[1]+'v2']
        with self.assertRaises(StorageError):
            store.refresh_discovery(fresh)
        self.assertEqual(self.snapshot(),before)

    def test_refresh_429_stops_without_mutating_metadata(self):
        self.arxiv_success(); self.s2_success(); self.run_on()
        self._age_cached_discovery()
        before=(self.paper_path()/'metadata.json').read_bytes()
        self.outcomes=[fx.http_error(429)]
        report=pipeline.run_selected([PAIR],self.root,timeout=15,max_attempts=1,revision_history=False)
        self.assertTrue(report['stopped'])
        self.assertEqual(report['stop_reason']['http_status'],429)
        self.assertEqual((self.paper_path()/'metadata.json').read_bytes(),before)
