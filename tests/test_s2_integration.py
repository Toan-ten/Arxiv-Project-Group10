"""Real S2/arXiv clients + Pipeline/Storage/Batch, fake transports only."""
import copy
import json
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import Mock, patch

import test_pipeline as fx
from test_semantic_scholar import Response, http_error, paper as s2_paper
from test_semantic_scholar_references import reference, page
from src import batch, benchmark, benchmark_runner, pipeline, s2_integration as adapter
from src import semantic_scholar as s2, storage

PAIR, OTHER, THIRD = fx.PAIR, fx.OTHER, fx.THIRD


class S2IntegrationTests(unittest.TestCase):
    sleep = fx.PipelineTests.sleep
    open_response = fx.PipelineTests.open_response
    store = fx.PipelineTests.store
    manifest = fx.PipelineTests.manifest
    paper_path = fx.PipelineTests.paper_path
    snapshot = fx.PipelineTests.snapshot
    seed = fx.PipelineTests.seed

    def setUp(self):
        fx.PipelineTests.setUp(self)
        self.s2_outcomes, self.s2_requests, self.s2_starts = [], [], []
        self.limiters = []
        original_init = s2.RateLimiter.__init__

        def track(instance, *args, **kwargs):
            original_init(instance, *args, **kwargs)
            self.limiters.append(instance)

        self.transport = Mock()
        self.transport.open.side_effect = self.s2_open
        for item in (
            patch.object(s2, "_opener", self.transport),
            patch.object(s2.RateLimiter, "__init__", track),
            patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": ""}),
            patch.object(socket, "create_connection", side_effect=AssertionError("Real network forbidden")),
            patch.object(socket.socket, "connect", side_effect=AssertionError("Real network forbidden")),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.close_remaining)

    def close_remaining(self):
        for outcome in self.outcomes + self.s2_outcomes:
            if hasattr(outcome, "close"):
                outcome.close()

    def s2_open(self, request, timeout):
        self.assertEqual(timeout, 15)
        self.assertTrue(self.limiters[-1]._lock.locked())
        self.s2_requests.append(request)
        self.s2_starts.append(self.now)
        self.assertTrue(self.s2_outcomes, "Unexpected S2 request")
        value = self.s2_outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def config(self, **changes):
        return dict(timeout=15, max_attempts=1, min_interval=2, **changes)

    def run_on(self, pairs=None, **changes):
        options = dict(timeout=15, max_attempts=1, versions=("v1",),
                       run_id="offline-fixture", s2_config=self.config())
        options.update(changes)
        return pipeline.run_selected([PAIR] if pairs is None else pairs, self.root, **options)

    def arxiv_success(self, pairs=(PAIR,), versions=1):
        self.outcomes.extend(r for pair in pairs for r in (
            fx.xml_response(pair[1], versions), fx.Response(fx.TEX)))

    def s2_success(self, pairs=(PAIR,), refs=None):
        for pair in pairs:
            self.s2_outcomes.extend([Response([s2_paper(pair[1])]),
                                     page([] if refs is None else refs)])

    def saved_metadata(self, status="found"):
        return dict(arxiv_id=PAIR[1], status=status, http_status=200, attempts=1,
                    paper=s2_paper(PAIR[1]) if status == "found" else None, error=None)

    def cache_metadata(self):
        store = self.store()
        store.save_metadata(*PAIR, adapter.metadata_snapshot(
            store.read_metadata(*PAIR), self.saved_metadata()))

    def partial(self):
        self.seed(completed=True)
        self.cache_metadata()
        self.s2_outcomes = [page([reference()], next=1)]
        value = s2.fetch_references(PAIR[1], timeout=15, max_attempts=1,
                                   limiter=s2.RateLimiter(2), limit=1, max_pages=1)
        self.store().save_references(*PAIR, value)
        self.s2_requests.clear()
        self.s2_starts.clear()
        return value

    def settings(self, **changes):
        result = dict(batch_id="s2-offline", max_ids=3, versions=("v1",),
                      timeout=15, max_attempts=1, s2_config=self.config(),
                      log_root=Path(self.temp.name) / "logs")
        result.update(changes)
        return result

    def run_batch(self, pairs=None, **changes):
        return batch.run_batch([PAIR] if pairs is None else pairs, self.root,
                               **self.settings(**changes))

    def aggregate(self):
        return benchmark_runner.aggregate_batch(
            self.root, batch_id="s2-offline", log_root=Path(self.temp.name) / "logs")

    def test_off_has_no_s2_requests_or_summary_changes(self):
        self.arxiv_success()
        result = self.run_on(s2_config=None)
        self.assertEqual(self.s2_requests, [])
        self.assertNotIn("s2_metadata", result["papers"][0])
        self.assertEqual(result["papers"][0]["references_status"], "not_attempted")

    def test_on_real_clients_complete_v1_keeps_source_and_ownership(self):
        self.arxiv_success()
        raws = [reference(), dict(citedPaper=None)]
        self.s2_success(refs=raws)
        value = self.run_on()
        m = self.store().read_metadata(*PAIR)
        refs = self.store().read_references(*PAIR)
        self.assertEqual((m["mssv"], m["arxiv_id"]), PAIR)
        self.assertEqual(m["authors"], ["Fixture Author"])
        self.assertEqual(m["submission_date"], fx.DATE)
        self.assertEqual([v["version"] for v in m["versions"]], ["v1"])
        self.assertEqual([r["raw"] for r in refs["items"]], raws)
        self.assertIsNone(refs["provenance"]["s2_paper_id"])
        self.assertEqual(value["papers"][0]["paper_status"], "complete")
        self.assertTrue(self.store().should_skip_version(*PAIR, "v1"))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.s2_requests), 2)

    def test_metadata_partial_only_dates_does_not_refetch_s2(self):
        self.arxiv_success(versions=3)
        self.s2_success()
        first = self.run_on()
        source = (self.paper_path() / "v1/main.tex").read_bytes()
        metadata = (self.paper_path() / "metadata.json").read_bytes()
        count = (len(self.requests), len(self.s2_requests))
        second = self.run_on()
        self.assertEqual((len(self.requests), len(self.s2_requests)), count)
        self.assertEqual(second["papers"][0]["s2_metadata"]["action"], "cached")
        self.assertEqual(second["papers"][0]["s2_references"]["action"], "cached")
        self.assertEqual(second["papers"][0]["pending"], ["v2", "v3"])
        self.assertEqual(first["papers"][0]["metadata_status"], "partial")
        self.assertEqual((self.paper_path() / "metadata.json").read_bytes(), metadata)
        self.assertEqual((self.paper_path() / "v1/main.tex").read_bytes(), source)

    def test_merge_does_not_replace_arxiv_fields_or_infer_dates(self):
        self.seed(versions=3)
        old = self.store().read_metadata(*PAIR)
        result = self.saved_metadata()
        result["paper"].update(title="Contradictory S2", authors=[{"name": "Different"}],
                               year=2020, publicationDate="2020-01-01", venue="Known venue")
        merged = adapter.metadata_snapshot(old, result)
        for key in ("mssv", "arxiv_id", "title", "authors", "versions",
                    "submission_date", "revised_dates"):
            self.assertEqual(merged[key], old[key])
        self.assertEqual(merged["provenance"]["arxiv"], old["provenance"]["arxiv"])
        self.assertEqual(merged["venue"], "Known venue")
        self.assertEqual(merged["status"], "partial")
        self.assertEqual(merged["provenance"]["semantic_scholar"]["snapshot"], result)

    def test_merge_fills_missing_title_and_authors(self):
        self.seed(versions=1)
        value = self.store().read_metadata(*PAIR)
        value.update(title=None, authors=None)
        merged = adapter.metadata_snapshot(value, self.saved_metadata())
        self.assertEqual(merged["title"], "Offline fixture")
        self.assertEqual(merged["authors"], ["Fixture"])
        self.assertEqual(merged["status"], "complete")

    def test_partial_author_names_do_not_become_complete_metadata(self):
        self.seed(versions=1)
        value = self.store().read_metadata(*PAIR)
        value["authors"] = None
        response = self.saved_metadata()
        response["paper"]["authors"] = [{"name": "Known"}, {"authorId": "missing"}]
        merged = adapter.metadata_snapshot(value, response)
        self.assertIsNone(merged["authors"])
        self.assertIn("authors", merged["missing_fields"])

    def test_fresh_metadata_wrong_or_missing_identity_is_not_used(self):
        for identity in ("2508.07872", None):
            with self.subTest(identity=identity):
                self.root = Path(self.temp.name) / ("identity-" + str(identity))
                self.arxiv_success()
                p = s2_paper(PAIR[1])
                p["externalIds"] = {} if identity is None else {"ArXiv": identity}
                self.s2_outcomes = [Response([p])]
                result = self.run_on()
                self.assertIn("s2_metadata", result["papers"][0]["blocked"])
                self.assertEqual(self.store().read_metadata(*PAIR)["title"], "Offline fixture")
                self.assertEqual(self.store().read_references(*PAIR)["status"], "not_attempted")

    def test_cache_metadata_identity_bad_blocks_before_http_or_recovery_write(self):
        self.seed(completed=True)
        self.cache_metadata()
        path = self.paper_path() / "metadata.json"
        data = json.loads(path.read_text())
        data["provenance"]["semantic_scholar"]["snapshot"]["paper"]["externalIds"]["ArXiv"] = OTHER[1]
        path.write_text(json.dumps(data), encoding="utf-8")
        before = self.snapshot()
        result = self.run_on()
        self.assertIn("paper", result["papers"][0]["blocked"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.requests, self.s2_requests), ([], []))

    def test_references_complete_wrong_query_blocks_before_any_request(self):
        self.arxiv_success()
        self.s2_success()
        self.run_on()
        path = self.paper_path() / "references.json"
        data = json.loads(path.read_text())
        data["provenance"]["query_identifier"] = "ARXIV:" + OTHER[1]
        path.write_text(json.dumps(data), encoding="utf-8")
        before, counts = self.snapshot(), (len(self.requests), len(self.s2_requests))
        self.assertIn("paper", self.run_on()["papers"][0]["blocked"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((len(self.requests), len(self.s2_requests)), counts)

    def test_partial_resume_replays_prefix_without_duplicate_or_lost_items(self):
        old = self.partial()
        second = reference("2508.07872")
        self.s2_outcomes = [page([reference()], next=1), page([second], offset=1)]
        result = self.run_on(s2_config=self.config(reference_limit=1))
        new = self.store().read_references(*PAIR)
        self.assertEqual(new["items"][:1], old["items"])
        self.assertEqual([r["raw"] for r in new["items"]], [reference(), second])
        self.assertEqual(new["status"], "complete")
        self.assertEqual(result["papers"][0]["s2_references"]["action"], "resumed")
        self.assertEqual(len(self.s2_requests), 2)

    def test_changed_prefix_blocks_and_preserves_prior_snapshot_bytes(self):
        self.partial()
        path = self.paper_path() / "references.json"
        before = path.read_bytes()
        self.s2_outcomes = [page([reference(title="Changed")])]
        result = self.run_on()
        self.assertIn("s2_references", result["papers"][0]["blocked"])
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_continuation_and_parent_identity_block_without_write(self):
        for change in ("continuation", "parent"):
            with self.subTest(change=change):
                self.root = Path(self.temp.name) / change
                self.partial()
                path = self.paper_path() / "references.json"
                data = json.loads(path.read_text())
                if change == "continuation":
                    data["coverage"]["continuation"] = {"offset": 999}
                else:
                    data["provenance"]["s2_paper_id"] = "parent-s2-id"
                path.write_text(json.dumps(data), encoding="utf-8")
                before, count = self.snapshot(), len(self.s2_requests)
                result = self.run_on()
                self.assertIn("paper", result["papers"][0]["blocked"])
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(len(self.s2_requests), count)

    def test_second_paper_metadata_429_stops_third_and_preserves_first(self):
        self.arxiv_success((PAIR, OTHER))
        self.s2_success()
        self.s2_outcomes.append(http_error(429))
        result = self.run_batch([PAIR, OTHER, THIRD])
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stop_reason"]["stage"], "s2_metadata")
        self.assertEqual(result["stop_reason"]["http_status"], 429)
        self.assertEqual(result["papers"][2]["state"], "pending")
        self.assertEqual((len(self.requests), len(self.s2_requests)), (4, 3))
        self.assertTrue(self.store().should_skip_version(*PAIR, "v1"))
        self.assertEqual(self.store().read_metadata(*OTHER)["status"], "failed")
        self.assertEqual(self.store().read_references(*OTHER)["status"], "not_attempted")
        audit = self.aggregate()
        self.assertFalse(audit["publication_complete"]["all_complete"])
        self.assertEqual(audit["history"]["attempts"][0]["stop_reason"]["stage"], "s2_metadata")
        self.assertIn("s2_metadata", [e["stage"] for e in audit["history"]["error_history"]])

    def test_second_paper_references_429_stops_third_after_checkpoint(self):
        self.arxiv_success((PAIR, OTHER), versions=3)
        self.s2_success()
        self.s2_outcomes.extend([Response([s2_paper(OTHER[1])]), http_error(429)])
        result = self.run_batch([PAIR, OTHER, THIRD])
        self.assertEqual(result["stop_reason"]["stage"], "s2_references")
        self.assertTrue(result["stop_reason"]["checkpoint_saved"])
        self.assertEqual((len(self.requests), len(self.s2_requests)), (4, 4))
        refs = self.store().read_references(*OTHER)
        self.assertEqual(refs["status"], "failed")
        self.assertEqual(refs["error"]["http_status"], 429)
        self.assertEqual(self.store().read_manifest(*OTHER)["versions"]["v2"]["status"], "pending")
        self.assertEqual(result["papers"][2]["state"], "pending")

    def test_references_429_between_pages_saves_partial(self):
        self.arxiv_success()
        self.s2_outcomes = [Response([s2_paper(PAIR[1])]),
                            page([reference()], next=1), http_error(429)]
        result = self.run_on(s2_config=self.config(reference_limit=1))
        refs = self.store().read_references(*PAIR)
        self.assertTrue(result["stopped"])
        self.assertEqual(refs["coverage"]["continuation"], {"offset": 1})
        self.assertEqual(len(refs["items"]), 1)
        self.assertFalse(refs["coverage"]["complete"])
        self.assertEqual(len(self.s2_requests), 3)

    def test_policy_stop_survives_storage_failure_for_both_s2_stages(self):
        for stage, exception in (("metadata", storage.StorageError("fixture")),
                                 ("references", OSError("fixture"))):
            with self.subTest(stage=stage):
                self.root = Path(self.temp.name) / stage
                self.arxiv_success()
                self.s2_outcomes = ([http_error(429)] if stage == "metadata" else [
                    Response([s2_paper(PAIR[1])]), http_error(429)])
                method = "save_" + stage
                original = getattr(storage.CorpusStorage, method)

                def failing(store, mssv, identifier, value):
                    result = (value["provenance"]["semantic_scholar"] or {}).get("snapshot", {}) if stage == "metadata" else value
                    if adapter.http_status(result, references=stage == "references") == 429:
                        raise exception
                    return original(store, mssv, identifier, value)

                with patch.object(storage.CorpusStorage, method, failing):
                    result = self.run_on([PAIR, OTHER])
                self.assertTrue(result["stopped"])
                self.assertFalse(result["stop_reason"]["checkpoint_saved"])
                self.assertEqual(result["stop_reason"]["http_status"], 429)
                self.assertEqual(len(result["papers"]), 1)

    def test_s2_403_also_stops_before_other_paper(self):
        self.arxiv_success()
        self.s2_outcomes = [http_error(403)]
        result = self.run_on([PAIR, OTHER])
        self.assertEqual(result["stop_reason"]["http_status"], 403)
        self.assertEqual(len(self.s2_requests), 1)

    def test_s2_not_found_is_distinct_from_arxiv_not_found_and_empty(self):
        self.arxiv_success()
        self.s2_outcomes = [Response([None]), http_error(404)]
        result = self.run_on()
        manifest = self.manifest()
        self.assertFalse(result["stopped"])
        self.assertEqual(manifest["discovery_status"], "found")
        self.assertEqual(manifest["metadata_status"], "not_found")
        self.assertEqual(manifest["references_status"], "not_found")
        self.assertEqual(manifest["status"], "partial")

    def test_arxiv_not_found_has_no_s2_calls(self):
        self.outcomes = [fx.Response(b'<feed xmlns="http://www.w3.org/2005/Atom"/>', "application/atom+xml")]
        self.run_on()
        self.assertEqual(self.s2_requests, [])

    def test_arxiv_policy_stop_has_no_s2_calls(self):
        self.outcomes = [fx.http_error(429)]
        self.assertTrue(self.run_on()["stopped"])
        self.assertEqual(self.s2_requests, [])

    def test_corrupt_source_is_not_overwritten_or_followed_by_s2(self):
        self.seed(completed=True)
        source = self.paper_path() / "v1/main.tex"
        source.write_bytes(b"corrupt fixture")
        result = self.run_on()
        self.assertEqual(source.read_bytes(), b"corrupt fixture")
        self.assertEqual(self.s2_requests, [])
        self.assertEqual(result["papers"][0]["source"]["v1"]["status"], "validation_failed")

    def test_shared_limiter_across_metadata_references_and_papers(self):
        self.arxiv_success((PAIR, OTHER))
        self.s2_success((PAIR, OTHER))
        self.run_on([PAIR, OTHER])
        self.assertEqual(len(self.limiters), 1)
        self.assertEqual(len(self.s2_requests), 4)
        self.assertTrue(all(b - a >= 2 for a, b in zip(self.s2_starts, self.s2_starts[1:])))

    def test_invalid_options_and_ownership_fail_before_all_io(self):
        for config in (False, {"api_key": "forbidden"}, {"max_pages": 0},
                       {"max_attempts": True}, {"min_interval": 0}, {"timeout": float("nan")}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.run_on(s2_config=config)
        with self.assertRaises(ValueError):
            self.run_on([(OTHER[0], PAIR[1])])
        self.assertFalse(self.root.exists())
        self.assertEqual((self.requests, self.s2_requests), ([], []))

    def test_interrupted_before_metadata_resumes_existing_source(self):
        self.seed(versions=1, completed=True)
        with patch.object(s2, "fetch_metadata", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_on()
        self.assertEqual(self.store().read_metadata(*PAIR)["status"], "in_progress")
        self.s2_success()
        result = self.run_on()
        self.assertEqual(result["papers"][0]["source"]["v1"]["action"], "skipped")
        self.assertEqual(self.requests, [])
        self.assertEqual(result["papers"][0]["paper_status"], "complete")

    def test_interrupted_after_metadata_skips_it_on_resume(self):
        self.seed(versions=1, completed=True)
        self.s2_outcomes = [Response([s2_paper(PAIR[1])])]
        with patch.object(s2, "fetch_references", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_batch()
        metadata = (self.paper_path() / "metadata.json").read_bytes()
        self.s2_outcomes = [page([])]
        value = self.run_batch(resume=True)
        self.assertEqual(value["papers"][0]["result"]["s2_metadata"]["action"], "cached")
        self.assertEqual((self.paper_path() / "metadata.json").read_bytes(), metadata)
        self.assertEqual(len(self.s2_requests), 2)
        self.assertEqual(self.aggregate()["publication_complete"]["papers"], 1)

    def test_interrupt_between_metadata_json_and_manifest_is_reconciled(self):
        self.seed(versions=1, completed=True)
        self.s2_outcomes = [Response([s2_paper(PAIR[1])])]
        original = storage._atomic_json
        interrupted = []

        def fail_once(path, value):
            metadata = self.store().read_metadata(*PAIR)
            if path.name == "manifest.json" and metadata["provenance"]["semantic_scholar"] and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
            return original(path, value)

        with patch.object(storage, "_atomic_json", side_effect=fail_once):
            with self.assertRaises(KeyboardInterrupt):
                self.run_on()
        self.assertEqual(self.store().read_metadata(*PAIR)["status"], "complete")
        self.s2_outcomes = [page([])]
        self.assertEqual(self.run_on()["papers"][0]["paper_status"], "complete")
        self.assertEqual(len(self.s2_requests), 2)

    def test_interrupted_mid_pagination_preserves_previous_partial(self):
        self.partial()
        path = self.paper_path() / "references.json"
        before = path.read_bytes()
        self.s2_outcomes = [page([reference()], next=1), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_on()
        self.assertEqual(path.read_bytes(), before)
        self.s2_outcomes = [page([reference()], next=1), page([reference("2508.07872")], offset=1)]
        self.run_on()
        self.assertEqual(len(self.store().read_references(*PAIR)["items"]), 2)

    def test_atomic_replace_failure_preserves_partial_snapshot(self):
        self.partial()
        path = self.paper_path() / "references.json"
        before = path.read_bytes()
        self.s2_outcomes = [page([reference()])]
        original = storage.os.replace

        def fail_refs(source, destination):
            if Path(destination) == path:
                raise OSError("fixture replace failure")
            return original(source, destination)

        with patch.object(storage.os, "replace", side_effect=fail_refs):
            with self.assertRaises(pipeline.PipelineStopped):
                self.run_on()
        self.assertEqual(path.read_bytes(), before)

    def test_batch_definition_rejects_enable_or_config_change_on_resume(self):
        self.arxiv_success()
        self.run_batch(s2_config=None)
        with self.assertRaisesRegex(batch.BatchError, "definition changed"):
            self.run_batch(resume=True)
        self.assertEqual(self.s2_requests, [])
        legacy = self.aggregate()
        self.assertEqual(legacy["status"], "verified")
        self.assertNotIn("s2", legacy["configuration"])

    def test_attempts_aggregate_unique_data_and_preserve_timeout_history(self):
        self.arxiv_success(versions=3)
        self.s2_outcomes = [Response([s2_paper(PAIR[1])]), page([reference()], next=1), TimeoutError()]
        self.run_batch()
        first = self.aggregate()
        self.assertIsNone(first["data_coverage"]["reference_items"])
        self.s2_outcomes = [page([reference()], next=1), page([reference("2508.07872")], offset=1)]
        self.run_batch(resume=True)
        report = self.aggregate()
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["data_coverage"]["valid_tex_versions"], 1)
        self.assertEqual(report["data_coverage"]["reference_items"], 2)
        self.assertEqual(report["history"]["operations"]["s2_metadata_queried"], 1)
        self.assertEqual(report["history"]["operations"]["s2_metadata_cached"], 1)
        self.assertIn("timeout", [e["type"] for e in report["history"]["error_history"]])
        self.assertFalse(report["publication_complete"]["all_complete"])
        self.assertEqual(len(self.requests), 2)

    def test_callbacks_still_only_started_and_finished(self):
        self.arxiv_success()
        self.s2_success()
        events = []
        self.run_on(on_progress=lambda event, summary: events.append(event))
        self.assertEqual(events, ["paper_started", "paper_finished"])

    def test_secret_is_not_persisted_in_corpus_logs_or_reports(self):
        key = "offline-secret-key-never-persist"
        self.arxiv_success()
        self.s2_success()
        with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": key}):
            self.run_batch()
            self.assertTrue(all(r.get_header("X-api-key") == key for r in self.s2_requests))
            report = self.aggregate()
        text = json.dumps(report) + "".join(
            p.read_text(encoding="utf-8") for p in Path(self.temp.name).rglob("*.json"))
        self.assertNotIn(key, text)
        self.assertNotIn("x-api-key", text.lower())

    def test_benchmark_propagates_config_and_measures_s2_inside_callback(self):
        self.arxiv_success(versions=3)
        self.s2_success()
        measured = []

        def foundation(call, **kwargs):
            call()
            measured.append(len(self.s2_requests))
            value = dict(status="success", elapsed_seconds=1.0, peak_ram_mb=1)
            Path(kwargs["report_path"]).write_text(json.dumps(value), encoding="utf-8")
            return value

        with patch.object(benchmark, "benchmark_pipeline", side_effect=foundation):
            report = benchmark_runner.run_benchmark(
                [PAIR], self.root, report_root=Path(self.temp.name) / "reports",
                **self.settings())
        self.assertEqual(measured, [2])
        self.assertEqual(report["configuration"]["s2"], adapter.configuration(self.config()))
        self.assertEqual(report["batch_status"], "finished")
        self.assertFalse(report["publication_complete"]["all_complete"])
        self.assertEqual(report["aggregation"]["history"]["http_metrics_status"], "collected")

    def test_complete_caches_are_byte_identical_after_resume(self):
        self.arxiv_success()
        self.s2_success(refs=[reference(), reference()])
        self.run_on()
        paths = [self.paper_path() / name for name in ("metadata.json", "references.json")]
        before = [p.read_bytes() for p in paths]
        counts = (len(self.requests), len(self.s2_requests))
        self.run_on()
        self.assertEqual([p.read_bytes() for p in paths], before)
        self.assertEqual((len(self.requests), len(self.s2_requests)), counts)

    def test_missing_metadata_response_part_is_failure_not_wrong_assignment(self):
        self.arxiv_success()
        self.s2_outcomes = [Response([])]
        value = self.run_on()
        self.assertEqual(value["papers"][0]["s2_metadata"]["status"], "invalid_response")
        self.assertEqual(len(self.s2_requests), 1)
        self.assertEqual(self.store().read_metadata(*PAIR)["status"], "failed")

    def test_non_arxiv_references_and_duplicates_survive_storage(self):
        self.arxiv_success()
        raw = reference()
        raw["citedPaper"]["externalIds"] = {}
        self.s2_success(refs=[raw, raw, {"citedPaper": None}])
        self.run_on()
        refs = self.store().read_references(*PAIR)
        self.assertEqual(len(refs["items"]), 3)
        self.assertTrue(all(r["arxiv_id"] is None for r in refs["items"]))
        self.assertEqual(refs["items"][0], refs["items"][1])

    def test_failed_resume_does_not_shrink_partial_references(self):
        previous = self.partial()
        self.s2_outcomes = [TimeoutError()]
        result = self.run_on()
        saved = self.store().read_references(*PAIR)
        self.assertEqual(saved["items"], previous["items"])
        self.assertEqual(saved["coverage"]["continuation"], {"offset": 1})
        self.assertEqual(result["papers"][0]["s2_metadata"]["action"], "cached")
        self.assertEqual(saved["error"]["type"], "timeout")

    def test_bad_reference_raw_is_blocked_without_overwrite(self):
        self.partial()
        path = self.paper_path() / "references.json"
        value = json.loads(path.read_text())
        value["items"][0]["title"] = "Forged normalization"
        path.write_text(json.dumps(value), encoding="utf-8")
        before = self.snapshot()
        self.run_on()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.s2_requests, [])

    def test_corrupt_checkpoint_blocks_before_s2(self):
        self.seed(completed=True)
        path = self.paper_path() / "manifest.json"
        path.write_bytes(b'{"broken"')
        before = self.snapshot()
        value = self.run_on()
        self.assertIn("paper", value["papers"][0]["blocked"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.s2_requests, [])

    def test_same_batch_cannot_change_s2_settings_on_resume(self):
        self.arxiv_success()
        self.s2_success()
        self.run_batch()
        with self.assertRaisesRegex(batch.BatchError, "definition changed"):
            self.run_batch(resume=True, s2_config=self.config(max_pages=2))
        self.assertEqual(len(self.s2_requests), 2)

    def test_secret_in_response_is_quarantined_not_saved(self):
        self.arxiv_success()
        response = s2_paper(PAIR[1])
        response["title"] = "offline-response-secret-key"
        self.s2_outcomes = [Response([response])]
        with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": "offline-response-secret-key"}):
            value = self.run_on()
        self.assertEqual(value["papers"][0]["s2_metadata"]["status"], "invalid_response")
        self.assertNotIn("offline-response-secret-key", json.dumps(
            self.store().read_metadata(*PAIR)))

    def test_s2_failure_on_one_paper_allows_next_independent_paper(self):
        self.arxiv_success((PAIR, OTHER))
        self.s2_outcomes = [TimeoutError(), page([])]
        self.s2_success((OTHER,))
        result = self.run_on([PAIR, OTHER])
        self.assertFalse(result["stopped"])
        self.assertEqual(len(result["papers"]), 2)
        self.assertEqual(result["papers"][1]["paper_status"], "complete")
        self.assertEqual(result["papers"][0]["metadata_status"], "failed")

    def test_references_403_stops_before_next_paper(self):
        self.arxiv_success()
        self.s2_outcomes = [Response([s2_paper(PAIR[1])]), http_error(403)]
        result = self.run_on([PAIR, OTHER])
        self.assertEqual(result["stop_reason"]["stage"], "s2_references")
        self.assertEqual(result["stop_reason"]["http_status"], 403)
        self.assertEqual(len(result["papers"]), 1)
        self.assertEqual(len(self.s2_requests), 2)

    def test_unresolved_versions_remain_unknown_even_after_s2_success(self):
        self.outcomes = [fx.xml_response(versions=None)]
        self.s2_success()
        value = self.run_on()
        m = self.store().read_metadata(*PAIR)
        self.assertEqual(m["version_status"], "needs_verification")
        self.assertEqual(m["versions"], [])
        self.assertEqual(value["papers"][0]["source"], {})
        self.assertEqual(value["papers"][0]["paper_status"], "partial")
        self.assertFalse((self.paper_path() / "v1").exists())
        self.assertEqual(len(self.requests), 1)

    def test_page_cap_is_partial_and_classified_not_cursor_progress(self):
        self.arxiv_success(versions=3)
        self.s2_outcomes = [Response([s2_paper(PAIR[1])]), page([reference()], next=1)]
        self.run_batch(s2_config=self.config(reference_limit=1, max_pages=1))
        report = self.aggregate()
        self.assertIn("pagination_limit", [e["type"] for e in report["history"]["error_history"]])
        self.assertIsNone(report["data_coverage"]["reference_items"])
        self.s2_outcomes = [page([reference()], next=1)]
        self.run_batch(resume=True, s2_config=self.config(reference_limit=1, max_pages=1))
        refs = self.store().read_references(*PAIR)
        self.assertEqual(refs["status"], "partial")
        self.assertEqual(len(refs["items"]), 1)
        self.assertEqual(len(self.s2_requests), 3)

    def test_interrupted_retry_of_not_found_references_preserves_resumability(self):
        self.seed(completed=True)
        self.cache_metadata()
        self.s2_outcomes = [http_error(404)]
        self.run_on()
        path = self.paper_path() / "references.json"
        before = path.read_bytes()
        with patch.object(s2, "fetch_references", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_on()
        self.assertEqual(path.read_bytes(), before)
        self.s2_outcomes = [page([])]
        value = self.run_on()
        self.assertEqual(value["papers"][0]["references_status"], "complete")
        self.assertEqual(len(self.s2_requests), 2)

    def test_intermediate_429_retry_success_remains_client_owned(self):
        self.arxiv_success()
        self.s2_outcomes = [http_error(429), Response([s2_paper(PAIR[1])]), page([])]
        config = self.config()
        config["max_attempts"] = 2
        value = self.run_on(s2_config=config)
        self.assertFalse(value["stopped"])
        self.assertEqual(value["papers"][0]["s2_metadata"]["attempts"], 2)
        self.assertEqual(len(self.s2_requests), 3)


if __name__ == "__main__":
    unittest.main()

