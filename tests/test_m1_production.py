"""Production operator wiring: fake HTTP, real Batch/Storage, TEMP only."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_pipeline as fx
from test_arxiv_history import entry, DATES
from src import arxiv_client, benchmark, benchmark_runner, id_ranges, semantic_scholar
from src.storage import CorpusStorage

ASSETS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("production_operator", ASSETS / "scripts/m1_production.py")
production = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(production)
COMMIT = "a" * 40


class ProductionSelectionTests(unittest.TestCase):
    def test_all_200_cohorts_cover_exactly_20000_unique_owned_positions(self):
        seen, counts = set(), {}
        expected = id_ranges.iter_assigned_ids()
        for index in range(1, 201):
            pairs = production.cohort_pairs(index)
            self.assertEqual(len(pairs), 100)
            for pair in pairs:
                self.assertEqual(pair, next(expected))
                self.assertNotIn(pair[1], seen)
                seen.add(pair[1])
                counts[pair[0]] = counts.get(pair[0], 0) + 1
        self.assertEqual(len(seen), 20000)
        self.assertEqual(counts, {m: 5000 for m in id_ranges.load_config()})
        self.assertEqual(list(expected), [])

    def test_cohort_boundaries_and_repeatability(self):
        self.assertEqual(production.cohort_pairs(1)[0], ("24120010", "2508.07872"))
        self.assertEqual(production.cohort_pairs(50)[-1], ("24120010", "2508.12871"))
        self.assertEqual(production.cohort_pairs(51)[0], ("24120035", "2507.16659"))
        self.assertEqual(production.cohort_pairs(200)[-1], ("24120260", "2501.07641"))
        self.assertEqual(production.cohort_pairs(120), production.cohort_pairs(120))

    def test_invalid_cohort_inputs_rejected(self):
        for value in (0, 201, True, None, "1", 1.5):
            with self.subTest(value=value), self.assertRaises(production.ProductionError):
                production.cohort_pairs(value)

    def test_notebook_has_two_plain_unexecuted_code_cells(self):
        nb = json.loads((ASSETS / "notebooks/m1_colab_arxiv_production.ipynb").read_text(encoding="utf-8"))
        cells = [c for c in nb["cells"] if c["cell_type"] == "code"]
        self.assertEqual(len(cells), 2)
        for cell in cells:
            compile("".join(cell["source"]), cell["id"], "exec")
            self.assertIsNone(cell["execution_count"])
            self.assertEqual(cell["outputs"], [])
        text = "\n".join("".join(c["source"]) for c in nb["cells"])
        for forbidden in ("ipywidgets", "userdata.get", "SEMANTIC_SCHOLAR_API_KEY", "--live"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(text.count('"first_cohort":'), 1)
        self.assertEqual(text.count('"last_cohort":'), 1)
        self.assertNotIn("True", "".join(cells[1]["source"]))


class ProductionRunTests(unittest.TestCase):
    sleep = fx.PipelineTests.sleep
    open_response = fx.PipelineTests.open_response

    def setUp(self):
        fx.PipelineTests.setUp(self)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.drive = Path(self.temp.name) / "drive-production"
        self.patches = [patch.object(production, "COHORT_SIZE", 2),
            patch.object(production, "_code_state", return_value=(COMMIT, "")),
            patch.object(semantic_scholar._opener, "open", side_effect=AssertionError("No S2 allowed")),
            patch.object(benchmark, "benchmark_pipeline", side_effect=self.fake_benchmark)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        redirect = contextlib.redirect_stdout(io.StringIO())
        self.output = redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.run = self.operator()

    def operator(self, **settings):
        return production.ProductionRun(self.repo, self.drive, COMMIT, timeout=15, **settings)

    def fake_benchmark(self, call, *, report_path, **kwargs):
        call()
        value = dict(status="success", elapsed_seconds=1.0, ram_source="mock",
                     peak_ram_mb=10, disk_corpus_size_mb=0.01)
        Path(report_path).write_text(json.dumps(value), encoding="utf-8")
        return value

    def success(self, cohort=1):
        self.outcomes.extend(response for pair in production.cohort_pairs(cohort)
                             for response in (fx.xml_response(pair[1]), fx.Response(fx.TEX)))

    def snapshot(self, root=None):
        root = self.drive if root is None else root
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def first_paper(self):
        return CorpusStorage(self.run.corpus).paper_path(*production.cohort_pairs(1)[0])

    def test_new_cohort_runs_all_versions_s2_off_with_measured_adapter(self):
        self.success()
        original = benchmark_runner.run_benchmark
        with patch.object(benchmark_runner, "run_benchmark", wraps=original) as measured:
            result = self.run.run()[0]
        self.assertTrue(result["arxiv_phase_complete"])
        self.assertEqual(result["mode"], "new")
        call = measured.call_args
        self.assertIsNone(call.kwargs["versions"])
        self.assertIsNone(call.kwargs["s2_config"])
        self.assertTrue(call.kwargs["revision_history"])
        self.assertEqual(call.kwargs["max_attempts"], 1)
        self.assertFalse(call.kwargs["resume"])
        self.assertEqual(call.kwargs["disk_roots"], [self.run.corpus, self.run.scratch])
        self.assertEqual(len(self.requests), 4)
        self.assertFalse(result["aggregation"]["publication_complete"]["all_complete"])
        self.assertTrue(Path(result["audit_path"]).is_file())

    def test_completed_cohort_is_audit_only_zero_http_and_no_new_attempt(self):
        self.success()
        self.run.run()
        protected = {name: self.snapshot(getattr(self.run, name)) for name in ("corpus", "logs", "reports")}
        with patch.object(benchmark_runner, "run_benchmark", side_effect=AssertionError("Audit only")):
            result = self.operator().run()[0]
        self.assertEqual(result["mode"], "audit")
        self.assertEqual(len(self.requests), 4)
        for name in protected:
            self.assertEqual(protected[name], self.snapshot(getattr(self.run, name)))

    def test_real_benchmark_samples_temp_corpus_and_counts_fake_transport(self):
        self.patches[-1].stop()  # Existing resource engine; HTTP is still fake.
        self.success()
        result = self.run.run()[0]
        report = json.loads(Path(result["benchmark_report_path"]).read_text(encoding="utf-8"))
        metrics = report["resource_metrics"]["metrics_v2"]
        self.assertGreater(metrics["ram"]["sample_count"], 0)
        self.assertGreater(metrics["disk"]["final_corpus_bytes"], 0)
        self.assertEqual(metrics["disk"]["roots"], [str(self.run.corpus), str(self.run.scratch)])
        self.assertEqual(result["aggregation"]["history"]["http_metrics"]["known"]["attempts"], 4)
        self.assertTrue(result["arxiv_phase_complete"])

    def test_finished_timeout_resumes_only_missing_source_and_keeps_good_source(self):
        first, second = production.cohort_pairs(1)
        self.outcomes = [fx.xml_response(first[1]), fx.Response(fx.TEX), fx.xml_response(second[1]), TimeoutError("offline")]
        initial = self.run.run()[0]
        self.assertFalse(initial["arxiv_phase_complete"])
        self.assertEqual(initial["batch_status"], "finished")
        before = self.snapshot(self.first_paper())
        self.outcomes = [fx.Response(fx.TEX)]
        resumed = self.operator().run()[0]
        self.assertEqual(resumed["mode"], "resume")
        self.assertTrue(resumed["arxiv_phase_complete"])
        # Manifest run_id/updated_at may correctly advance on Resume; metadata,
        # references and valid source bytes must remain unchanged.
        after = self.snapshot(self.first_paper())
        self.assertEqual({k: v for k, v in after.items() if k != "manifest.json"},
                         {k: v for k, v in before.items() if k != "manifest.json"})
        self.assertTrue(CorpusStorage(self.run.corpus).should_skip_version(*first, "v1"))
        self.assertEqual(len(self.requests), 5)
        self.assertTrue(any(e["type"] == "timeout" for e in resumed["aggregation"]["history"]["error_history"]))

    def test_403_and_429_stop_without_next_paper_version_or_cohort(self):
        for status in (403, 429):
            with self.subTest(status=status):
                self.drive = Path(self.temp.name) / f"drive-{status}"
                run = self.operator()
                first = production.cohort_pairs(1)[0]
                self.outcomes = [fx.xml_response(first[1], 2), fx.http_error(status)]
                count = len(self.requests)
                results = run.run(1, 2)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["batch_status"], "stopped")
                self.assertEqual(results[0]["stop_reason"]["http_status"], status)
                self.assertFalse(results[0]["arxiv_phase_complete"])
                self.assertEqual(len(self.requests) - count, 2)
                manifest = CorpusStorage(run.corpus).read_manifest(*first)
                self.assertEqual(manifest["versions"]["v2"]["status"], "pending")

    def test_not_found_is_resolved_candidate_absence_not_publication_complete(self):
        first, second = production.cohort_pairs(1)
        self.outcomes = [self.not_found(), fx.xml_response(second[1]), fx.Response(fx.TEX)]
        result = self.run.run()[0]
        self.assertTrue(result["arxiv_phase_complete"])
        self.assertEqual(result["aggregation"]["data_coverage"]["not_found_papers"], 1)
        self.assertFalse(result["aggregation"]["publication_complete"]["all_complete"])
        self.assertEqual(result["history_checks"][0]["revision_history"], "not_applicable")

    @staticmethod
    def not_found():
        return fx.Response(b'<feed xmlns="http://www.w3.org/2005/Atom" '
            b'xmlns:o="http://a9.com/-/spec/opensearch/1.1/"><o:totalResults>0</o:totalResults></feed>',
            "application/atom+xml")

    def test_http_404_is_not_confirmed_absence_and_keeps_cohort_incomplete(self):
        pair = production.cohort_pairs(1)[1]
        self.outcomes = [fx.http_error(404), fx.xml_response(pair[1]), fx.Response(fx.TEX)]
        result = self.run.run()[0]
        self.assertFalse(result["arxiv_phase_complete"])
        self.assertEqual(result["aggregation"]["data_coverage"]["not_found_papers"], 0)
        self.assertEqual(len(self.requests), 3)

    def test_three_versions_and_dates_verified_then_audit_only(self):
        pair = production.cohort_pairs(1)[0]
        self.outcomes = [fx.xml_response(pair[1], 3)] + [fx.Response(fx.TEX) for _ in range(3)]
        self.outcomes += [fx.Response(entry(f"v{i}", pair[1]), "application/atom+xml") for i in range(1, 4)]
        self.outcomes += [self.not_found()]
        result = self.run.run()[0]
        self.assertTrue(result["arxiv_phase_complete"])
        metadata = CorpusStorage(self.run.corpus).read_metadata(*pair)
        self.assertEqual([v["date"] for v in metadata["versions"]], DATES)
        self.assertEqual(metadata["revised_dates"], DATES[1:])
        self.assertEqual(result["aggregation"]["data_coverage"]["valid_tex_versions"], 3)
        self.assertEqual(len(self.requests), 8)
        before = self.snapshot(self.run.corpus)
        self.assertEqual(self.operator().run()[0]["mode"], "audit")
        self.assertEqual(len(self.requests), 8)
        self.assertEqual(self.snapshot(self.run.corpus), before)

    def test_history_timeout_resumes_history_without_redownloading_sources(self):
        pair = production.cohort_pairs(1)[0]
        self.outcomes = [fx.xml_response(pair[1], 2), fx.Response(fx.TEX), fx.Response(fx.TEX),
                         fx.Response(entry("v1", pair[1]), "application/atom+xml"),
                         TimeoutError("offline history"), self.not_found()]
        first = self.run.run()[0]
        self.assertFalse(first["arxiv_phase_complete"])
        store = CorpusStorage(self.run.corpus)
        self.assertIsNone(store.read_metadata(*pair)["revised_dates"])
        before = {p.relative_to(self.run.corpus).as_posix(): p.read_bytes()
                  for p in self.run.corpus.rglob("*.tex")}
        self.outcomes = [fx.Response(entry(v, pair[1]), "application/atom+xml") for v in ("v1", "v2")]
        result = self.operator().run()[0]
        self.assertTrue(result["arxiv_phase_complete"])
        self.assertEqual(len(self.requests), 8)
        self.assertEqual(store.read_metadata(*pair)["revised_dates"], DATES[1:2])
        self.assertEqual(before, {p.relative_to(self.run.corpus).as_posix(): p.read_bytes()
                                 for p in self.run.corpus.rglob("*.tex")})

    def test_invalid_history_provenance_blocks_without_rewriting_checkpoint(self):
        pair = production.cohort_pairs(1)[0]
        self.outcomes = [fx.xml_response(pair[1], 2), fx.Response(fx.TEX), fx.Response(fx.TEX)]
        self.outcomes += [fx.Response(entry(v, pair[1]), "application/atom+xml") for v in ("v1", "v2")]
        self.outcomes += [self.not_found()]
        self.run.run()
        path = self.first_paper() / "metadata.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["provenance"]["version_history"]["snapshot"][0]["raw_xml"] = "<feed/>"
        path.write_text(json.dumps(value), encoding="utf-8")
        before, requests = self.snapshot(), len(self.requests)
        with self.assertRaisesRegex(production.ProductionError, "history provenance"):
            self.operator().run()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.requests), requests)

    def test_no_tex_terminal_source_is_counted_separately(self):
        pair = production.cohort_pairs(1)[0]
        self.outcomes = [fx.xml_response(pair[1]), fx.Response(b"%PDF-1.4\nfixture", "application/pdf"),
                         self.not_found()]
        result = self.run.run()[0]
        self.assertTrue(result["arxiv_phase_complete"])
        self.assertEqual(result["aggregation"]["data_coverage"]["no_tex_versions"], 1)
        self.assertEqual(result["aggregation"]["data_coverage"]["valid_tex_versions"], 0)

    def test_discovery_policy_stop_does_not_start_next_paper_or_cohort(self):
        self.outcomes = [fx.http_error(429)]
        results = self.run.run(1, 2)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["batch_status"], "stopped")
        self.assertEqual(len(self.requests), 1)

    def test_interrupted_version_without_destination_resumes_safely(self):
        pair, second = production.cohort_pairs(1)
        self.outcomes = [fx.xml_response(pair[1]), TimeoutError("offline"), self.not_found()]
        self.run.run()
        store = CorpusStorage(self.run.corpus)
        destination = store.begin_version(*pair, "v1")
        self.assertFalse(destination.exists())
        self.outcomes = [fx.Response(fx.TEX)]
        result = self.operator().run()[0]
        self.assertTrue(result["arxiv_phase_complete"])
        self.assertTrue(store.should_skip_version(*pair, "v1"))
        self.assertEqual(len(self.requests), 4)

    def test_complete_window_advances_only_over_explicit_cohorts(self):
        self.success(1)
        self.success(2)
        results = self.run.run(1, 2)
        self.assertEqual([r["cohort"] for r in results], [1, 2])
        self.assertTrue(all(r["arxiv_phase_complete"] for r in results))
        self.assertEqual(len(self.requests), 8)
        self.assertFalse((self.run.logs / "m1-arxiv-0003").exists())

    def test_unresolved_discovery_does_not_invent_versions_or_advance_cohort(self):
        self.outcomes = [fx.Response(fx.atom(production.cohort_pairs(1)[0][1], None), "application/atom+xml"),
                         fx.http_error(404)]
        results = self.run.run(1, 2)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["arxiv_phase_complete"])
        self.assertEqual(len(self.requests), 2)
        self.assertFalse((self.first_paper() / "v1").exists())

    def test_checksum_corruption_blocks_without_network_or_checkpoint_write(self):
        self.success()
        self.run.run()
        (self.first_paper() / "v1" / "main.tex").write_bytes(b"corrupt offline fixture")
        before = self.snapshot()
        with patch.object(benchmark_runner, "run_benchmark") as measured:
            with self.assertRaises(production.ProductionError):
                self.operator().run()
            measured.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_malformed_log_preserved_and_no_request(self):
        self.success()
        self.run.run()
        log = next((self.run.logs / "m1-arxiv-0001" / "attempts").glob("*.json"))
        log.write_bytes(b"{broken offline")
        before = self.snapshot()
        with self.assertRaises(production.ProductionError):
            self.operator().run()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.requests), 4)

    def test_orphan_checkpoint_without_batch_logs_is_never_overwritten(self):
        self.drive.mkdir()
        from src.storage import _atomic_json
        _atomic_json(self.drive / "campaign.json", self.run.definition)
        path = self.first_paper() / "v1"
        path.mkdir(parents=True)
        (path / "main.tex").write_bytes(fx.TEX)
        before = self.snapshot()
        with self.assertRaisesRegex(production.ProductionError, "without batch"):
            self.run.run()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.requests, [])

    def test_existing_pilot_or_comparison_directory_is_refused(self):
        self.drive.mkdir()
        (self.drive / "pilot-evidence.json").write_bytes(b"preserve")
        before = self.snapshot()
        with self.assertRaisesRegex(production.ProductionError, "Nonempty"):
            self.operator()
        self.assertEqual(self.snapshot(), before)

    def test_changed_campaign_configuration_rejected_before_requests(self):
        self.success()
        self.run.run()
        before = self.snapshot()
        with self.assertRaisesRegex(production.ProductionError, "configuration changed"):
            self.operator(disk_interval=60)
        self.assertEqual(self.snapshot(), before)

    def test_changed_batch_definition_rejected_without_repair(self):
        self.success()
        self.run.run()
        path = self.run.logs / "m1-arxiv-0001" / "batch.json"
        value = json.loads(path.read_text())
        value["definition"]["versions"] = ["v1"]
        path.write_text(json.dumps(value))
        before = self.snapshot()
        with self.assertRaisesRegex(production.ProductionError, "Batch definition"):
            self.operator().run()
        self.assertEqual(self.snapshot(), before)

    def test_dirty_or_wrong_commit_rejected_before_writes(self):
        for value in ((COMMIT, " M src/pipeline.py"), ("b" * 40, "")):
            with patch.object(production, "_code_state", return_value=value):
                with self.assertRaises(production.ProductionError):
                    self.operator()
        self.assertFalse(self.drive.exists())

    def test_data_inside_checkout_or_ancestor_is_rejected(self):
        for root in (self.repo / "data", self.repo.parent):
            with self.assertRaises(production.ProductionError):
                production.ProductionRun(self.repo, root, COMMIT)

    def test_invalid_window_and_numeric_settings_make_no_files(self):
        for first, last in ((2, 1), (0, 1), (1, 201), (True, 1)):
            with self.assertRaises(production.ProductionError):
                self.run.run(first, last)
        for value in (True, -1, float("nan"), "1"):
            with self.assertRaises(production.ProductionError):
                self.operator(sample_interval=value)
        self.assertFalse(self.drive.exists())

    def test_subprocess_temp_and_environment_restored_on_exception(self):
        old_temp, old_env = tempfile.tempdir, os.environ.get("TMPDIR")
        with patch.object(benchmark_runner, "run_benchmark", side_effect=OSError("offline write failure")) as measured:
            with self.assertRaises(OSError):
                self.run.run()
            measured.assert_called_once()
        self.assertEqual(tempfile.tempdir, old_temp)
        self.assertEqual(os.environ.get("TMPDIR"), old_env)
        self.assertFalse(production._WRITER.locked())

    def test_same_process_concurrent_entry_refused_before_writes(self):
        production._WRITER.acquire()
        try:
            with self.assertRaises(production.ProductionError):
                self.run.run()
        finally:
            production._WRITER.release()
        self.assertFalse(self.drive.exists())

    def test_production_run_passes_configurable_max_attempts(self):
        op = production.ProductionRun(self.repo, self.drive, COMMIT, max_attempts=3)
        self.assertEqual(op.settings["max_attempts"], 3)


if __name__ == "__main__":
    unittest.main()
