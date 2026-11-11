"""Version dates and arXiv-first enrichment: fake HTTP, TEMP only."""
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch

import test_pipeline as fx
from test_s2_integration import S2IntegrationTests as _S2Fixtures
from src import arxiv_history as history, batch
from src.storage import StorageError

PAIR, DATE = fx.PAIR, fx.DATE
DATES = [DATE, "2025-02-13T10:00:00Z", "2025-02-14T11:00:00Z"]


def entry(version="v1", identifier=PAIR[1], date=None, published=DATE):
    date = DATES[int(version[1:]) - 1] if date is None else date
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        f'<id>https://arxiv.org/abs/{identifier}{version}</id>'
        f'<published>{published}</published><updated>{date}</updated>'
        '</entry></feed>'
    ).encode()


class HistoryTests(unittest.TestCase):
    setUp = fx.PipelineTests.setUp
    sleep = fx.PipelineTests.sleep
    open_response = fx.PipelineTests.open_response
    store = fx.PipelineTests.store
    seed = fx.PipelineTests.seed
    snapshot = fx.PipelineTests.snapshot
    paper_path = fx.PipelineTests.paper_path

    def fetch(self, **options):
        return history.enrich_version_history(self.store(), *PAIR, timeout=15, **options)

    def queue_history(self, n=3):
        self.outcomes = [fx.Response(entry(f"v{i}"), "application/atom+xml")
                         for i in range(1, n + 1)]

    def test_all_dates_and_provenance_saved_without_touching_sources(self):
        self.seed(versions=3, completed=True)
        source = (self.paper_path() / "v1/main.tex").read_bytes()
        self.queue_history()
        value = self.fetch()
        metadata = self.store().read_metadata(*PAIR)
        self.assertEqual(value["status"], "complete")
        self.assertEqual(metadata["revised_dates"], DATES[1:])
        self.assertEqual([v["date"] for v in metadata["versions"]], DATES)
        self.assertTrue(all(v["date_status"] == "verified" for v in metadata["versions"]))
        self.assertEqual(metadata["mssv"], PAIR[0])
        self.assertEqual(metadata["provenance"]["version_history"]["snapshot"], value["records"])
        self.assertEqual((self.paper_path() / "v1/main.tex").read_bytes(), source)
        self.assertTrue(self.store().should_skip_version(*PAIR, "v1"))
        self.assertFalse((self.paper_path() / "v2").exists())
        self.assertEqual(self.starts, [0, 3, 6])
        queries = [parse_qs(urlsplit(u).query) for u in self.requests]
        self.assertEqual([q["id_list"] for q in queries], [[PAIR[1] + v] for v in ("v1", "v2", "v3")])
        self.assertEqual(metadata["status"], "partial")
        self.assertEqual(self.store().read_references(*PAIR)["status"], "not_attempted")

    def test_valid_history_cached_without_requests_or_json_writes(self):
        self.seed(versions=3)
        self.queue_history()
        self.fetch()
        before, count = self.snapshot(), len(self.requests)
        self.assertEqual(self.fetch()["action"], "cached")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.requests), count)

    def test_final_papers_projection_has_exact_schema(self):
        self.seed(versions=3)
        self.queue_history()
        self.fetch()
        row = history.paper_row(self.store(), *PAIR)
        self.assertEqual(set(row), {"arxiv_id", "title", "authors", "submission_date", "revised_dates", "venue"})
        self.assertEqual(row["revised_dates"], DATES[1:])
        self.assertIsNone(row["venue"])

    def test_unknown_revisions_do_not_export_as_empty(self):
        self.seed(versions=3)
        with self.assertRaises(StorageError):
            history.paper_row(self.store(), *PAIR)

    def test_one_confirmed_version_can_export_empty_revisions(self):
        self.seed(versions=1)
        self.assertEqual(history.paper_row(self.store(), *PAIR)["revised_dates"], [])

    def test_parser_accepts_old_style_exact_id(self):
        identifier = "hep-th/9901001"
        result = history.parse_version_response(entry("v2", identifier), identifier + "v2")
        self.assertEqual(result["versioned_id"], identifier + "v2")
        self.assertEqual(result["date"], DATES[1])

    def test_parser_rejects_missing_wrong_or_ambiguous_evidence(self):
        valid = entry("v2").decode()
        samples = [
            entry("v3"), valid.replace(PAIR[1], fx.OTHER[1]),
            valid.replace('<updated>', '<wrong>').replace('</updated>', '</wrong>'),
            valid.replace(DATES[1], "2025"), valid.replace(DATES[1], "2025-02-13T10:00:00"),
            valid.replace(DATES[1], "2025-02-11T10:00:00Z"),
            valid.replace('<entry>', '<entry><id>https://arxiv.org/abs/' + PAIR[1] + 'v2</id>'),
            valid.replace('https://arxiv.org/abs/', 'https://example.com/abs/'),
            valid.replace('</entry>', '<updated>' + DATES[1] + '</updated></entry>'),
            '<feed/>', '<broken', bytes([255]),
            '<!DOCTYPE feed [<!ENTITY e "x">]>' + valid,
            valid.replace('</entry>', '<link rel="alternate" href="https://arxiv.org/abs/' + PAIR[1] + 'v3"/></entry>'),
            valid.replace('<entry>', '<link rel="self" href="https://export.arxiv.org/api/query?id_list=' + PAIR[1] + 'v3"/><entry>'),
            b'x' * (history.MAX_XML_BYTES + 1), None,
        ]
        for xml in samples:
            with self.subTest(xml=str(xml)[:100]), self.assertRaises(StorageError):
                history.parse_version_response(xml, PAIR[1] + "v2")

    def test_parser_preserves_exact_timezone_instant(self):
        result = history.parse_version_response(entry("v2", date="2025-02-13T12:00:00+02:00"), PAIR[1] + "v2")
        self.assertEqual(result["date"], DATES[1])

    def test_v1_published_updated_disagreement_rejected(self):
        with self.assertRaises(StorageError):
            history.parse_version_response(entry("v1", date=DATES[1]), PAIR[1] + "v1")

    def test_http_policy_errors_stop_without_checkpoint_writes(self):
        self.seed(versions=3)
        for status in (403, 429):
            with self.subTest(status=status):
                before, count = self.snapshot(), len(self.requests)
                self.outcomes = [fx.Response(entry(), "application/atom+xml"), fx.http_error(status)]
                value = self.fetch()
                self.assertTrue(value["stopped"])
                self.assertEqual(value["http_status"], status)
                self.assertEqual(value["status"], "partial")
                self.assertEqual(len(value["records"]), 1)
                self.assertEqual(len(self.requests) - count, 2)
                self.assertEqual(self.snapshot(), before)

    def test_timeout_or_http404_never_becomes_empty_history(self):
        self.seed(versions=3)
        for outcome in (TimeoutError(), fx.http_error(404)):
            with self.subTest(outcome=type(outcome).__name__):
                before = self.snapshot()
                self.outcomes = [outcome]
                result = self.fetch()
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["stopped"])
                self.assertEqual(self.snapshot(), before)

    def test_latest_fallback_or_invalid_mime_does_not_save(self):
        self.seed(versions=3)
        for response in (fx.Response(entry("v3"), "application/atom+xml"), fx.Response(b'<html/>', "text/html")):
            with self.subTest(response=response):
                before = self.snapshot()
                self.outcomes = [response]
                self.assertEqual(self.fetch()["error"]["type"], "invalid_response")
                self.assertEqual(self.snapshot(), before)

    def test_conflicting_published_date_preserves_all_checkpoint_bytes(self):
        self.seed(versions=3)
        before = self.snapshot()
        self.queue_history()
        self.outcomes[1] = fx.Response(entry("v2", published="2025-02-11T09:00:00Z"), "application/atom+xml")
        with self.assertRaisesRegex(StorageError, "conflicting submission"):
            self.fetch()
        self.assertEqual(self.snapshot(), before)

    def test_conflicting_existing_date_preserves_checkpoint(self):
        self.seed(versions=3)
        metadata = self.store().read_metadata(*PAIR)
        metadata["versions"][1].update(date="2025-02-13T10:00:01Z", date_status="verified")
        self.store().save_metadata(*PAIR, metadata)
        before = self.snapshot()
        self.queue_history()
        with self.assertRaisesRegex(StorageError, "known version date"):
            self.fetch()
        self.assertEqual(self.snapshot(), before)

    def test_revision_dates_must_be_in_version_order(self):
        self.seed(versions=3)
        self.queue_history()
        self.outcomes[2] = fx.Response(entry("v3", date="2025-02-12T10:00:00Z"), "application/atom+xml")
        before = self.snapshot()
        with self.assertRaisesRegex(StorageError, "revision order"):
            self.fetch()
        self.assertEqual(self.snapshot(), before)

    def test_inconsistent_checkpoint_rejected_before_requests_and_writes(self):
        self.seed(versions=3)
        path = self.paper_path() / 'manifest.json'
        manifest = json.loads(path.read_text())
        del manifest['versions']['v2']
        path.write_text(json.dumps(manifest), encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(StorageError):
            self.fetch()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.snapshot(), before)

    def test_damaged_source_rejected_before_history_fetch(self):
        self.seed(versions=3, completed=True)
        (self.paper_path() / 'v1/main.tex').write_bytes(b'corrupt fixture')
        before = self.snapshot()
        with self.assertRaises(StorageError):
            self.fetch()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.snapshot(), before)

    def test_damaged_history_cache_is_not_silently_refetched(self):
        self.seed(versions=3)
        self.queue_history()
        self.fetch()
        path = self.paper_path() / 'metadata.json'
        metadata = json.loads(path.read_text())
        metadata['provenance']['version_history']['snapshot'][1]['date'] = DATES[0]
        path.write_text(json.dumps(metadata), encoding='utf-8')
        before, count = self.snapshot(), len(self.requests)
        with self.assertRaises(StorageError):
            self.fetch()
        self.assertEqual(len(self.requests), count)
        self.assertEqual(self.snapshot(), before)

    def test_unknown_provenance_requires_audit_instead_of_overwrite(self):
        self.seed(versions=3)
        metadata = self.store().read_metadata(*PAIR)
        metadata['provenance']['version_history'] = {'provider': 'some-other-source'}
        self.store().save_metadata(*PAIR, metadata)
        before = self.snapshot()
        with self.assertRaises(StorageError):
            self.fetch()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.snapshot(), before)

    def test_bad_options_and_ownership_fail_before_requests(self):
        self.seed(versions=3)
        before = self.snapshot()
        for option in ({'timeout': 0}, {'max_attempts': 0}, {'user_agent': chr(10)}, {'max_attempts': True}):
            with self.subTest(option=option), self.assertRaises(ValueError):
                history.enrich_version_history(self.store(), *PAIR, **option)
        with self.assertRaises(StorageError):
            history.enrich_version_history(self.store(), fx.OTHER[0], PAIR[1])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.snapshot(), before)


    def test_finite_retry_uses_shared_transport_and_closes_http_error(self):
        self.seed(versions=2)
        error = fx.http_error(503)
        self.outcomes = [error, fx.Response(entry(), "application/atom+xml"),
                         fx.Response(entry("v2"), "application/atom+xml")]
        result = self.fetch(max_attempts=2)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["records"][0]["attempts"], 2)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.starts, [0, 3, 6])
        self.assertTrue(error.closed)

    def test_atomic_metadata_failure_keeps_all_original_checkpoint_bytes(self):
        self.seed(versions=3)
        before = self.snapshot()
        self.queue_history()
        from src import storage
        with patch.object(storage.os, 'replace', side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):
                self.fetch()
        self.assertEqual(self.snapshot(), before)

    def test_history_for_v1_only_and_cached_reads(self):
        self.seed(versions=1)
        self.assertEqual(self.fetch()['status'], 'complete')
        self.assertEqual(self.store().read_metadata(*PAIR)['revised_dates'], [])
        before = self.snapshot()
        self.assertEqual(self.fetch()['action'], 'cached')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.requests), 0)



class EnrichmentTests(unittest.TestCase):
    # Reuse fixtures without inheriting/re-running the old integration tests.
    setUp = _S2Fixtures.setUp
    sleep = _S2Fixtures.sleep
    open_response = _S2Fixtures.open_response
    store = _S2Fixtures.store
    manifest = _S2Fixtures.manifest
    paper_path = _S2Fixtures.paper_path
    snapshot = _S2Fixtures.snapshot
    seed = _S2Fixtures.seed
    s2_open = _S2Fixtures.s2_open
    close_remaining = _S2Fixtures.close_remaining
    s2_success = _S2Fixtures.s2_success
    saved_metadata = _S2Fixtures.saved_metadata
    cache_metadata = _S2Fixtures.cache_metadata
    config = _S2Fixtures.config

    def test_history_completes_metadata_without_requerying_or_retimestamping_s2(self):
        self.seed(versions=3, completed=True)
        self.cache_metadata()
        before = copy.deepcopy(self.store().read_metadata(*PAIR)['provenance']['semantic_scholar'])
        self.outcomes = [fx.Response(entry(f'v{i}'), 'application/atom+xml') for i in range(1, 4)]
        history.enrich_version_history(self.store(), *PAIR, timeout=15)
        metadata = self.store().read_metadata(*PAIR)
        self.assertEqual(metadata['status'], 'complete')
        self.assertEqual(metadata['missing_fields'], [])
        self.assertEqual(metadata['provenance']['semantic_scholar'], before)
        self.assertEqual(self.s2_requests, [])
        self.assertEqual(self.manifest()['status'], 'partial')
        self.assertEqual(self.manifest()['versions']['v2']['status'], 'pending')

    def test_arxiv_first_all_versions_then_new_s2_batch_sends_no_arxiv_requests(self):
        self.outcomes = [fx.xml_response(PAIR[1], 3)] + [fx.Response(fx.TEX) for _ in range(3)]
        options = dict(max_ids=1, log_root=self.root.parent / 'logs', versions=None,
                       timeout=15, max_attempts=1, revision_history=False)
        first = batch.run_batch([PAIR], self.root, batch_id='arxiv-first', s2_config=None, **options)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(first['status'], 'finished')
        self.assertEqual(self.manifest()['references_status'], 'not_attempted')
        source = {p.as_posix(): p.read_bytes() for p in self.paper_path().rglob('*.tex')}
        old_logs = {p.as_posix(): p.read_bytes() for p in (self.root.parent / 'logs/arxiv-first').rglob('*.json')}
        self.s2_success()
        second = batch.run_batch([PAIR], self.root, batch_id='s2-enrichment', s2_config=self.config(), **options)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(len(self.s2_requests), 2)
        result = second['papers'][0]['result']
        self.assertEqual(result['discovery']['action'], 'cached')
        self.assertEqual(set(result['skipped']), {'v1', 'v2', 'v3'})
        self.assertEqual(self.manifest()['status'], 'partial')  # version dates remain unknown
        self.assertEqual(source, {p.as_posix(): p.read_bytes() for p in self.paper_path().rglob('*.tex')})
        self.assertTrue(all(Path(p).read_bytes() == body for p, body in old_logs.items()))
        third = batch.run_batch([PAIR], self.root, batch_id='s2-enrichment', s2_config=self.config(), resume=True, **options)
        self.assertEqual((len(self.requests), len(self.s2_requests)), (4, 2))
        self.assertEqual(third['papers'][0]['result']['s2_references']['action'], 'cached')


    def test_arxiv_first_then_s2_429_preserves_sources_and_stops_before_next_paper(self):
        pairs = [PAIR, fx.OTHER]
        self.outcomes = [r for pair in pairs for r in
                         (fx.xml_response(pair[1], 1), fx.Response(fx.TEX))]
        options = dict(max_ids=2, log_root=self.root.parent / 'logs', versions=None,
                       timeout=15, max_attempts=1, revision_history=False)
        batch.run_batch(pairs, self.root, batch_id='source-phase', s2_config=None, **options)
        source = {p.as_posix(): p.read_bytes() for p in self.root.rglob('*.tex')}
        from test_semantic_scholar import http_error
        self.s2_outcomes = [http_error(429)]
        result = batch.run_batch(pairs, self.root, batch_id='s2-phase', s2_config=self.config(), **options)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual((len(self.requests), len(self.s2_requests)), (4, 1))
        self.assertEqual(result['papers'][1]['state'], 'pending')
        self.assertEqual(source, {p.as_posix(): p.read_bytes() for p in self.root.rglob('*.tex')})
        for pair in pairs:
            self.assertTrue(self.store().should_skip_version(*pair, 'v1'))
        self.assertEqual(self.store().read_metadata(*fx.OTHER)['provenance']['semantic_scholar'], None)
        self.assertEqual(self.store().read_references(*PAIR)['status'], 'not_attempted')


    def test_complete_v1_reused_without_forcing_history_refresh(self):
        self.seed(versions=1, completed=True)
        self.cache_metadata()
        self.assertEqual(self.store().read_metadata(*PAIR)['status'], 'complete')
        before = self.snapshot()
        result = history.enrich_version_history(self.store(), *PAIR, timeout=15)
        self.assertEqual(result['action'], 'cached')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((len(self.requests), len(self.s2_requests)), (0, 0))

    def test_history_finishes_only_after_all_versions_metadata_and_references(self):
        self.outcomes = [fx.xml_response(PAIR[1], 3)] + [fx.Response(fx.TEX) for _ in range(3)]
        self.s2_success()
        options = dict(max_ids=1, log_root=self.root.parent / 'logs', versions=None,
                       timeout=15, max_attempts=1, s2_config=self.config(), revision_history=False)
        batch.run_batch([PAIR], self.root, batch_id='all-stages', **options)
        self.assertEqual(self.manifest()['status'], 'partial')
        self.outcomes = [fx.Response(entry(f'v{i}'), 'application/atom+xml') for i in range(1, 4)]
        history.enrich_version_history(self.store(), *PAIR, timeout=15)
        self.assertEqual(self.manifest()['status'], 'complete')
        self.assertEqual(self.store().read_metadata(*PAIR)['status'], 'complete')
        self.assertEqual(len(history.paper_row(self.store(), *PAIR)['revised_dates']), 2)
        count = len(self.requests), len(self.s2_requests)
        resumed = batch.run_batch([PAIR], self.root, batch_id='all-stages', resume=True, **options)
        self.assertEqual((len(self.requests), len(self.s2_requests)), count)
        self.assertEqual(resumed['papers'][0]['result']['s2_metadata']['action'], 'cached')
        self.assertEqual(resumed['papers'][0]['result']['s2_references']['action'], 'cached')
        self.assertEqual(set(resumed['papers'][0]['result']['skipped']), {'v1', 'v2', 'v3'})


# unittest discovers imported TestCase classes too; fixtures are not new tests.
del _S2Fixtures


if __name__ == '__main__':
    unittest.main()
