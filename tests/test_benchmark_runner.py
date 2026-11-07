"""Adapter tests use TEMP, fake HTTP and a mocked resource monitor by default."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_pipeline as fixtures
from src import batch, benchmark, benchmark_runner as runner, pipeline

PAIR, OTHER, THIRD = fixtures.PAIR, fixtures.OTHER, fixtures.THIRD
Response, TEX = fixtures.Response, fixtures.TEX
xml_response, http_error = fixtures.xml_response, fixtures.http_error


class BenchmarkRunnerTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    sleep = fixtures.PipelineTests.sleep
    open_response = fixtures.PipelineTests.open_response
    store = fixtures.PipelineTests.store
    manifest = fixtures.PipelineTests.manifest
    paper_path = fixtures.PipelineTests.paper_path
    snapshot = fixtures.PipelineTests.snapshot

    def settings(self):
        return dict(
            batch_id="offline-benchmark", max_ids=3,
            log_root=Path(self.temp.name) / "logs",
            report_root=Path(self.temp.name) / "reports",
            versions=("v1",), timeout=15, max_attempts=1, revision_history=False,
        )

    def fake_benchmark(self, call, *, corpus_output_dir, report_path, **kwargs):
        status = "success"
        try:
            call()
        except BaseException:
            status = "failed"
            raise
        finally:
            value = dict(status=status, elapsed_seconds=1.25,
                         peak_ram_mb=10, ram_source="mock",
                         disk_corpus_size_mb=0.01)
            Path(report_path).write_text(json.dumps(value), encoding="utf-8")
        return value

    def run_adapter(self, pairs=None, *, real_benchmark=False, **kwargs):
        settings = self.settings()
        settings.update(kwargs)
        if real_benchmark:
            return runner.run_benchmark([PAIR] if pairs is None else pairs, self.root, **settings)
        with patch.object(benchmark, "benchmark_pipeline", side_effect=self.fake_benchmark) as engine:
            result = runner.run_benchmark([PAIR] if pairs is None else pairs, self.root, **settings)
            engine.assert_called_once()
            return result

    def aggregate(self):
        return runner.aggregate_batch(
            self.root, batch_id=self.settings()["batch_id"],
            log_root=self.settings()["log_root"],
        )

    def successes(self, pairs=(PAIR,), versions=1):
        self.outcomes = [
            response for pair in pairs
            for response in (xml_response(pair[1], versions), Response(TEX))
        ]

    def attempt_paths(self):
        return sorted((self.settings()["log_root"] / self.settings()["batch_id"] / "attempts").glob("*.json"))

    def report(self, experiment):
        return json.loads((self.settings()["report_root"] / experiment / "report.json").read_text())

    def mutate(self, path, change):
        value = json.loads(path.read_text())
        change(value)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_finished_with_timeout_is_not_valid_source_or_complete_publication(self):
        self.outcomes = [xml_response(), TimeoutError("offline timeout")]
        report = self.run_adapter()
        self.assertEqual(report["execution_status"], "returned")
        self.assertEqual(report["batch_status"], "finished")
        self.assertEqual(report["resource_metrics"]["status"], "success")
        coverage = report["data_coverage"]
        self.assertEqual(coverage["valid_tex_versions"], 0)
        self.assertFalse(coverage["selected_sources_terminal"])
        self.assertFalse(report["publication_complete"]["all_complete"])
        self.assertEqual(coverage["version_statuses"], {"timeout": 1})

    def test_stopped_403_and_429_are_not_full_success(self):
        for code in (403, 429):
            with self.subTest(code=code):
                self.outcomes = [http_error(code)]
                result = self.run_adapter(batch_id=f"stop-{code}")
                self.assertEqual(result["batch_status"], "stopped")
                self.assertFalse(result["data_coverage"]["selected_sources_terminal"])
                self.assertFalse(result["publication_complete"]["all_complete"])
        self.assertEqual(self.opener.open.call_count, 2)

    def test_timeout_then_success_keeps_error_history_and_deduplicates(self):
        self.outcomes = [xml_response(), TimeoutError("old timeout")]
        first = self.run_adapter(experiment_id="attempt-one")
        old_log = self.attempt_paths()[0].read_bytes()
        self.outcomes = [Response(TEX)]
        second = self.run_adapter(experiment_id="attempt-two", resume=True)
        coverage, history = second["data_coverage"], second["aggregation"]["history"]
        self.assertEqual(coverage["cohort_papers"], 1)
        self.assertEqual(coverage["valid_tex_versions"], 1)
        self.assertEqual(coverage["verified_source_bytes"], len(TEX))
        self.assertTrue(coverage["selected_sources_terminal"])
        self.assertEqual(history["operations"]["source_downloaded"], 1)
        self.assertEqual(history["operations"]["discovery_cached"], 1)
        self.assertTrue(any(e["type"] == "timeout" for e in history["error_history"]))
        self.assertEqual(len(history["attempts"]), 2)
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual(old_log, next(p for p in self.attempt_paths() if p.read_bytes() == old_log).read_bytes())
        self.assertEqual(first["data_coverage"]["valid_tex_versions"], 0)

    def test_resume_skip_does_not_count_download_or_size_twice(self):
        self.successes()
        self.run_adapter(experiment_id="first")
        source = (self.paper_path() / "v1" / "main.tex").read_bytes()
        second = self.run_adapter(experiment_id="second", resume=True)
        history, coverage = second["aggregation"]["history"], second["data_coverage"]
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(history["operations"]["source_downloaded"], 1)
        self.assertEqual(history["operations"]["source_skipped"], 1)
        self.assertEqual(coverage["valid_tex_versions"], 1)
        self.assertEqual(coverage["verified_source_bytes"], len(source))
        self.assertEqual(history["http_requests"], 2)
        self.assertIsNone(history["downloaded_bytes"])

    def test_versions_selection_keeps_all_versions_but_not_all_version_complete(self):
        self.successes(versions=3)
        report = self.run_adapter()
        coverage = report["data_coverage"]
        self.assertEqual(coverage["known_versions"], 3)
        self.assertEqual(coverage["version_statuses"], {"pending": 2, "success": 1})
        self.assertTrue(coverage["selected_sources_terminal"])
        self.assertFalse(coverage["all_version_sources_terminal"])

    def test_references_not_attempted_is_unknown_not_zero(self):
        self.successes()
        report = self.run_adapter()
        self.assertIsNone(report["data_coverage"]["reference_items"])
        self.assertIsNone(report["data_coverage"]["papers"][0]["reference_items"])
        self.assertEqual(report["data_coverage"]["references_complete_papers"], 0)
        self.assertEqual(report["publication_complete"]["papers"], 0)

    def test_complete_empty_references_count_zero_only_with_verified_coverage(self):
        self.successes()
        self.run_adapter()
        refs = self.store().read_references(*PAIR)
        refs.update(status="complete")
        refs["coverage"].update(complete=True, reported_total=0)
        refs["provenance"].update(query_identifier="ARXIV:" + PAIR[1], retrieved_at=fixtures.DATE)
        self.store().save_references(*PAIR, refs)
        result = self.aggregate()
        self.assertEqual(result["data_coverage"]["reference_items"], 0)
        self.assertFalse(result["publication_complete"]["all_complete"])

    def test_other_cohort_in_corpus_is_ignored(self):
        self.successes((OTHER,))
        pipeline.run_selected([OTHER], self.root, timeout=15, max_attempts=1)
        self.successes()
        report = self.run_adapter()
        self.assertEqual(report["data_coverage"]["cohort_papers"], 1)
        self.assertEqual(report["data_coverage"]["valid_tex_versions"], 1)
        self.assertEqual(report["data_coverage"]["verified_source_bytes"], len(TEX))

    def test_missing_source_and_checksum_mismatch_are_not_success(self):
        self.successes()
        self.run_adapter()
        source = self.paper_path() / "v1" / "main.tex"
        for change in ("corrupt", "missing"):
            with self.subTest(change=change):
                if change == "corrupt":
                    source.write_bytes(b"invalid")
                else:
                    source.unlink()
                result = self.aggregate()
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(result["data_coverage"]["valid_tex_versions"], 0)
                self.assertFalse(result["publication_complete"]["all_complete"])

    def test_inconsistent_checkpoint_is_reported_without_any_repair(self):
        self.successes()
        self.run_adapter()
        path = self.paper_path() / "manifest.json"
        self.mutate(path, lambda v: v.update(references_status="partial"))
        before = self.snapshot()
        result = self.aggregate()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(self.snapshot(), before)

    def test_unjustified_complete_claim_rejected(self):
        self.successes()
        self.run_adapter()
        self.mutate(self.paper_path() / "manifest.json", lambda v: v.update(status="complete"))
        result = self.aggregate()
        self.assertEqual(result["data_coverage"]["invalid_checkpoints"], 1)
        self.assertFalse(result["publication_complete"]["all_complete"])

    def test_pending_orphan_is_not_valid_data_and_is_preserved(self):
        self.successes(versions=2)
        self.run_adapter()
        orphan = self.paper_path() / "v2"
        orphan.mkdir()
        (orphan / "main.tex").write_bytes(TEX)
        before = self.snapshot()
        self.assertEqual(self.aggregate()["status"], "incomplete")
        self.assertEqual(self.snapshot(), before)

    def test_missing_logs_return_unknown_coverage(self):
        result = self.aggregate()
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["data_coverage"])
        self.assertTrue(result["issues"])
        self.assertFalse(self.settings()["log_root"].exists())

    def test_empty_attempt_history_not_complete_even_if_source_exists(self):
        self.successes()
        self.run_adapter()
        for path in self.attempt_paths():
            path.unlink()
        result = self.aggregate()
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["data_coverage"])

    def test_malformed_log_and_wrong_status_types_fail_transparently(self):
        self.successes()
        self.run_adapter()
        path = self.attempt_paths()[0]
        original = path.read_bytes()
        for content in ('{', '{"status": []}', None):
            with self.subTest(content=content):
                path.write_bytes(original)
                if content is None:
                    self.mutate(path, lambda a: a.update(wall_seconds=float("nan")))
                else:
                    path.write_text(content, encoding="utf-8")
                result = self.aggregate()
                self.assertEqual(result["status"], "incomplete")
                self.assertIsNone(result["data_coverage"])

    def test_missing_sequence_and_missing_tail_referenced_by_checkpoint_detected(self):
        self.successes()
        self.run_adapter()
        self.run_adapter(resume=True)
        paths = self.attempt_paths()
        earlier = next(p for p in paths if json.loads(p.read_text())["attempt_number"] == 1)
        later = next(p for p in paths if p != earlier)
        original = earlier.read_bytes()
        earlier.unlink()
        self.assertIsNone(self.aggregate()["data_coverage"])
        earlier.write_bytes(original)
        later.unlink()
        # The latest paper run_id points to the missing tail attempt.
        self.assertEqual(self.aggregate()["status"], "incomplete")

    def test_wall_time_sums_intervals_not_session_gaps_or_resource_snapshots(self):
        self.successes()
        self.run_adapter()
        self.run_adapter(resume=True)
        for path in self.attempt_paths():
            value = json.loads(path.read_text())
            value["wall_seconds"] = 2.5 if value["attempt_number"] == 1 else 4.0
            value["papers"][0]["wall_seconds"] = 1.5
            # Deliberately separated sessions. No subtraction of calendar timestamps.
            value["started_at"] = "2026-01-01T00:00:00+00:00" if value["attempt_number"] == 1 else "2026-02-01T00:00:00+00:00"
            path.write_text(json.dumps(value), encoding="utf-8")
        history = self.aggregate()["history"]
        self.assertEqual(history["logged_attempt_wall_seconds"], 6.5)
        self.assertEqual(history["paper_wall_seconds"][0]["logged_wall_seconds"], 3.0)

    def test_running_attempt_time_is_explicit_lower_bound(self):
        self.successes()
        self.run_adapter()
        self.mutate(self.attempt_paths()[0], lambda v: v.update(status="running", ended_at=None))
        result = self.aggregate()
        self.assertEqual(result["history"]["wall_status"], "lower_bound")

    def test_original_batch_exception_reraised_and_failed_report_preserved(self):
        secret = "secret-token-not-for-report"
        with patch.object(batch, "run_batch", side_effect=RuntimeError(secret)):
            with self.assertRaisesRegex(RuntimeError, secret):
                self.run_adapter(experiment_id="failed")
        report = self.report("failed")
        self.assertEqual(report["execution_status"], "raised")
        self.assertNotEqual(report["batch_status"], "finished")
        self.assertIsNone(report["data_coverage"])
        self.assertNotIn(secret, json.dumps(report))

    def test_real_foundation_sanitizes_exception_in_both_reports(self):
        secret = "api_key=secret-fixture"
        with patch.object(pipeline, "run_selected", side_effect=RuntimeError(secret)):
            with self.assertRaisesRegex(RuntimeError, secret):
                self.run_adapter(experiment_id="sanitized", real_benchmark=True, user_agent=secret)
        directory = self.settings()["report_root"] / "sanitized"
        for path in directory.glob("*.json"):
            self.assertNotIn(secret, path.read_text())
        report = self.report("sanitized")
        self.assertEqual(report["batch_status"], "failed")
        self.assertEqual(report["resource_metrics"]["status"], "failed")
        self.assertEqual(report["execution_status"], "raised")

    def test_measurement_failure_does_not_claim_success(self):
        with patch.object(benchmark, "benchmark_pipeline", side_effect=OSError("private details")):
            with self.assertRaises(OSError):
                runner.run_benchmark([PAIR], self.root, experiment_id="monitor-failed", **self.settings())
        result = self.report("monitor-failed")
        self.assertEqual(result["execution_status"], "raised")
        self.assertEqual(result["measurement_status"], "not_collected")
        self.assertIsNone(result["batch_status"])

    def test_exception_during_second_paper_keeps_first_source(self):
        self.outcomes = [xml_response(), Response(TEX), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_adapter([PAIR, OTHER], experiment_id="interrupt")
        report = self.report("interrupt")
        self.assertEqual(report["batch_status"], "interrupted")
        self.assertEqual(report["data_coverage"]["valid_tex_versions"], 1)
        self.assertFalse(report["publication_complete"]["all_complete"])
        self.assertTrue(self.store().should_skip_version(*PAIR, "v1"))

    def test_report_no_overwrite_before_any_request(self):
        self.successes()
        report = self.run_adapter(experiment_id="unique")
        directory = Path(report["report_path"]).parent
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        with self.assertRaises(runner.BenchmarkError):
            self.run_adapter(experiment_id="unique", resume=True)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_provenance_records_ids_config_and_environment_without_headers(self):
        self.successes()
        with patch.object(runner, "_environment", return_value=dict(
                git_commit="f" * 40, git_dirty=True, python="test", operating_system="test",
                measurement_environment="not_collected")):
            result = self.run_adapter(experiment_id="provenance", user_agent="private-header")
        self.assertEqual(len(result["attempt_ids"]), 1)
        self.assertEqual(len(result["run_ids"]), 1)
        self.assertEqual(result["environment"]["git_dirty"], True)
        self.assertEqual(result["configuration"]["selection"], [list(PAIR)])
        self.assertEqual(result["configuration"]["versions"], ["v1"])
        self.assertEqual(len(result["selection_fingerprint"]), 64)
        self.assertNotIn("private-header", json.dumps(result))
        self.assertIsNone(result["unavailable_metrics"]["average_ram_bytes"])

    def test_invalid_input_fails_before_any_output_or_http(self):
        cases = [
            ([PAIR, PAIR], {}), ([("wrong", PAIR[1])], {}),
            ([PAIR], dict(versions=("bad",))), ([PAIR], dict(max_ids=0)),
            ([PAIR], dict(sample_interval=float("nan"))),
            ([PAIR], dict(experiment_id="../outside")),
            ([PAIR], dict(report_root=self.root / "reports")),
        ]
        for pairs, options in cases:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.run_adapter(pairs, **options)
                self.assertFalse(self.root.exists())
                self.assertFalse(self.settings()["log_root"].exists())
                self.assertFalse(self.settings()["report_root"].exists())
        self.opener.open.assert_not_called()

    def test_aggregation_is_read_only_for_corpus_and_logs(self):
        self.successes()
        self.run_adapter()
        paths = list(self.root.rglob("*")) + list(self.settings()["log_root"].rglob("*"))
        before = {str(p): p.read_bytes() for p in paths if p.is_file()}
        self.aggregate()
        self.assertEqual(before, {str(p): p.read_bytes() for p in paths if p.is_file()})

    def test_three_paper_two_attempt_scenario_has_three_unique_successes(self):
        self.outcomes = [
            xml_response(PAIR[1]), Response(TEX),
            xml_response(OTHER[1]), Response(TEX),
            xml_response(THIRD[1]), TimeoutError("offline"),
        ]
        self.run_adapter([PAIR, OTHER, THIRD], experiment_id="three-first")
        self.outcomes = [Response(TEX)]
        result = self.run_adapter([PAIR, OTHER, THIRD], resume=True, experiment_id="three-second")
        self.assertEqual(result["data_coverage"]["cohort_papers"], 3)
        self.assertEqual(result["data_coverage"]["valid_tex_versions"], 3)
        self.assertEqual(result["data_coverage"]["verified_source_bytes"], 3 * len(TEX))
        self.assertEqual(result["publication_complete"]["papers"], 0)
        operations = result["aggregation"]["history"]["operations"]
        self.assertEqual(operations["source_downloaded"], 3)
        self.assertEqual(operations["source_skipped"], 2)
        self.assertEqual(self.opener.open.call_count, 7)

    def test_not_found_is_distinct_from_unknown_versions(self):
        self.outcomes = [Response(b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/"><o:totalResults>0</o:totalResults></feed>', "application/atom+xml")]
        result = self.run_adapter()
        self.assertEqual(result["data_coverage"]["not_found_papers"], 1)
        self.assertEqual(result["data_coverage"]["unresolved_papers"], 0)
        self.assertFalse(result["data_coverage"]["selected_sources_terminal"])
        self.assertEqual(self.opener.open.call_count, 1)

    def test_unresolved_discovery_does_not_imply_v1_or_source_success(self):
        self.outcomes = [xml_response(versions=None)]
        result = self.run_adapter()
        self.assertEqual(result["data_coverage"]["unresolved_papers"], 1)
        self.assertEqual(result["data_coverage"]["valid_tex_versions"], 0)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_no_tex_is_terminal_but_not_a_tex_success(self):
        self.outcomes = [xml_response(), Response(b"%PDF-1.4\nfixture", "application/pdf")]
        result = self.run_adapter()
        coverage = result["data_coverage"]
        self.assertEqual(coverage["no_tex_versions"], 1)
        self.assertEqual(coverage["valid_tex_versions"], 0)
        self.assertEqual(coverage["verified_source_files"], 0)
        self.assertTrue(coverage["selected_sources_terminal"])
        self.assertFalse(result["publication_complete"]["all_complete"])

    def test_default_all_versions_and_verified_publication_complete(self):
        self.outcomes = [xml_response(), Response(TEX)]
        self.run_adapter(versions=None)
        metadata = self.store().read_metadata(*PAIR)
        metadata.update(status="complete", missing_fields=[], revised_dates=[])
        metadata["provenance"]["semantic_scholar"] = {
            "query_identifier": "ARXIV:" + PAIR[1], "retrieved_at": fixtures.DATE,
            "snapshot": {"title": "Offline fixture"},
        }
        self.store().save_metadata(*PAIR, metadata)
        refs = self.store().read_references(*PAIR)
        refs.update(status="complete")
        refs["coverage"].update(complete=True, reported_total=0)
        refs["provenance"].update(query_identifier="ARXIV:" + PAIR[1], retrieved_at=fixtures.DATE)
        self.store().save_references(*PAIR, refs)
        result = self.aggregate()
        self.assertTrue(result["publication_complete"]["all_complete"])
        self.assertEqual(result["data_coverage"]["reference_items"], 0)

    def test_free_text_log_errors_are_never_copied_into_report(self):
        self.outcomes = [xml_response(), TimeoutError("offline")]
        self.run_adapter()
        secret = "not-a-public-error-key"
        def corrupt_label(value):
            error = value["papers"][0]["result"]["errors"][0]
            error.update(type=secret, stage=secret, message=secret)
        self.mutate(self.attempt_paths()[0], corrupt_label)
        result = self.aggregate()
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["history"]["error_history"][0]["type"], "unclassified")

    def test_stopped_source_keeps_prior_paper_and_reports_status_and_version(self):
        self.outcomes = [xml_response(), Response(TEX), xml_response(OTHER[1], 3), http_error(429)]
        result = self.run_adapter([PAIR, OTHER, THIRD])
        self.assertEqual(result["batch_status"], "stopped")
        self.assertEqual(result["stop_reason"]["http_status"], 429)
        self.assertEqual(result["stop_reason"]["arxiv_id"], OTHER[1])
        self.assertEqual(result["stop_reason"]["version"], "v1")
        self.assertEqual(result["data_coverage"]["valid_tex_versions"], 1)
        self.assertEqual(self.opener.open.call_count, 4)

    def test_log_write_failure_preserves_last_durable_status_and_raises(self):
        original = batch._save
        def fail_final(path, payload):
            if payload.get("status") == "finished":
                raise batch.BatchLogError("private credential text")
            return original(path, payload)
        self.successes()
        with patch.object(batch, "_save", side_effect=fail_final):
            with self.assertRaises(batch.BatchLogError):
                self.run_adapter(experiment_id="log-failed")
        result = self.report("log-failed")
        self.assertEqual(result["execution_status"], "raised")
        self.assertIsNone(result["batch_status"])
        self.assertEqual(result["durable_batch_status"], "running")
        self.assertEqual(result["aggregation"]["history"]["wall_status"], "lower_bound")
        self.assertNotIn("private credential text", json.dumps(result))

    def test_real_foundation_normal_return_is_execution_only(self):
        self.outcomes = [xml_response(), TimeoutError("offline")]
        result = self.run_adapter(real_benchmark=True)
        self.assertEqual(result["resource_metrics"]["status"], "success")
        self.assertGreaterEqual(result["resource_metrics"]["elapsed_seconds"], 0)
        self.assertFalse(result["data_coverage"]["selected_sources_terminal"])


    def test_unprocessed_paper_has_no_checkpoint_without_being_called_corrupt(self):
        self.outcomes = [http_error(429)]
        result = self.run_adapter([PAIR, OTHER])
        self.assertEqual(result["data_coverage"]["not_created_checkpoints"], 1)
        self.assertEqual(result["data_coverage"]["invalid_checkpoints"], 0)
        self.assertFalse(result["data_coverage"]["selected_sources_terminal"])
        self.assertFalse(self.paper_path(OTHER).exists())

    def test_deleted_previously_logged_paper_is_invalid_not_unprocessed(self):
        self.successes()
        self.run_adapter()
        for path in self.paper_path().rglob("*"):
            if path.is_file():
                path.unlink()
        (self.paper_path() / "v1").rmdir()
        self.paper_path().rmdir()
        result = self.aggregate()
        self.assertEqual(result["data_coverage"]["invalid_checkpoints"], 1)
        self.assertEqual(result["data_coverage"]["not_created_checkpoints"], 0)

    def test_unavailable_ram_is_null_in_adapter_and_foundation_stays_unchanged(self):
        original = self.fake_benchmark
        def unavailable(*args, **kwargs):
            result = original(*args, **kwargs)
            result.update(ram_source="unavailable", initial_ram_mb=0, final_ram_mb=0, peak_ram_mb=0)
            return result
        self.successes()
        with patch.object(benchmark, "benchmark_pipeline", side_effect=unavailable):
            result = runner.run_benchmark([PAIR], self.root, **self.settings())
        self.assertIsNone(result["resource_metrics"]["peak_ram_mb"])

    def test_corrupt_existing_history_rejected_before_new_experiment_or_request(self):
        self.successes()
        self.run_adapter(experiment_id="before")
        self.attempt_paths()[0].write_text("{", encoding="utf-8")
        with self.assertRaises(runner.BenchmarkError):
            self.run_adapter(experiment_id="after", resume=True)
        self.assertFalse((self.settings()["report_root"] / "after").exists())
        self.assertEqual(self.opener.open.call_count, 2)

    def test_adapter_default_versions_downloads_every_verified_version(self):
        self.outcomes = [xml_response(versions=2), Response(TEX), Response(TEX)]
        result = self.run_adapter(versions=None)
        self.assertEqual(result["data_coverage"]["valid_tex_versions"], 2)
        self.assertTrue(result["data_coverage"]["all_version_sources_terminal"])
        self.assertEqual(self.opener.open.call_count, 3)


if __name__ == "__main__":
    unittest.main()
