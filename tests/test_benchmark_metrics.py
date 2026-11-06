"""Metrics v2 fixtures only: fake HTTP, clocks, samplers and TEMP directories."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_pipeline as fixtures
from src import benchmark, benchmark_metrics as metrics, benchmark_runner as runner, pipeline


PAIR, OTHER = fixtures.PAIR, fixtures.OTHER
TEX, Response, xml_response, http_error = fixtures.TEX, fixtures.Response, fixtures.xml_response, fixtures.http_error


def resource_fixture(wall=5.0, ram_values=(10, 30, 20), peak_disk=40, final_corpus=15):
    def series(values):
        s = metrics._Series()
        for i, value in enumerate(values):
            s.add(value, float(i))
        return s.report()
    ram = series(ram_values)
    ram.update(source="psutil", scope="tree")
    disk = series((5, peak_disk, final_corpus))
    disk.pop("average_method")
    disk.pop("average_bytes")
    disk.update(roots=["fixture-root"], final_corpus_bytes=final_corpus)
    return dict(schema_version=2, wall_seconds=wall, ram=ram, disk=disk, sampling={"ram_interval_seconds": .1, "disk_interval_seconds": 30})


class SamplingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "corpus"
        self.tick = 0.0

    def test_average_peak_and_observation_times(self):
        values = iter([10, 40, 20])
        sampler = metrics.ResourceSampler(self.root, ram_reader=lambda: next(values), clock=lambda: self.tick)
        for t in (0.0, 2.0, 5.0):
            self.tick = t
            sampler.sample_ram()
        ram = sampler.report()["ram"]
        self.assertEqual(ram["sample_count"], 3)
        self.assertEqual(ram["average_bytes"], 70 / 3)
        self.assertEqual(ram["sampled_peak_bytes"], 40)
        self.assertEqual(ram["first_sample_seconds"], 0)
        self.assertEqual(ram["last_sample_seconds"], 5)
        self.assertEqual(ram["unit"], "bytes")

    def test_disk_peak_final_and_disjoint_scratch_scope(self):
        scratch = Path(self.temp.name) / "scratch"
        sizes = {self.root.resolve(): 10, scratch.resolve(): 20}
        sampler = metrics.ResourceSampler(
            self.root, disk_roots=(self.root, scratch), disk_reader=lambda p: sizes[p],
        )
        sampler.sample_disk()
        sizes.update({self.root.resolve(): 25, scratch.resolve(): 100})
        sampler.sample_disk()
        sizes.update({self.root.resolve(): 30, scratch.resolve(): 0})
        sampler.sample_disk(final=True)
        disk = sampler.report()["disk"]
        self.assertEqual(disk["sample_count"], 3)
        self.assertEqual(disk["sampled_peak_bytes"], 125)
        self.assertEqual(disk["final_bytes"], 30)
        self.assertEqual(disk["final_corpus_bytes"], 30)

    def test_scratch_scan_failure_does_not_discard_measured_final_corpus(self):
        scratch = Path(self.temp.name) / "scratch"
        def read(path):
            if path == self.root.resolve():
                return 25
            raise PermissionError("scratch unreadable")
        sampler = metrics.ResourceSampler(
            self.root, disk_roots=[self.root, scratch], disk_reader=read,
        )
        sampler.sample_disk(final=True)
        disk = sampler.report()["disk"]
        self.assertEqual(disk["status"], "not_collected")
        self.assertIsNone(disk["sampled_peak_bytes"])
        self.assertEqual(disk["final_corpus_bytes"], 25)
        self.assertEqual(disk["final_corpus_status"], "collected")

    def test_no_samples_are_unknown(self):
        value = metrics.ResourceSampler(self.root).report()
        for name in ("ram", "disk"):
            self.assertEqual(value[name]["status"], "not_collected")
            self.assertEqual(value[name]["sample_count"], 0)
            self.assertIsNone(value[name]["sampled_peak_bytes"])
        self.assertIsNone(value["ram"]["average_bytes"])

    def test_measurement_failures_not_zero_or_secret_messages(self):
        def fail(*args):
            raise PermissionError("secret-key-fixture")
        sampler = metrics.ResourceSampler(self.root, ram_reader=fail, disk_reader=fail)
        sampler.sample_ram()
        sampler.sample_disk(final=True)
        value = sampler.report()
        self.assertEqual(value["ram"]["status"], "not_collected")
        self.assertEqual(value["disk"]["final_corpus_status"], "not_collected")
        self.assertIsNone(value["disk"]["final_corpus_bytes"])
        self.assertNotIn("secret-key-fixture", json.dumps(value))

    def test_partial_series_records_missing_observations(self):
        outcomes = iter([100, None, 200])
        sampler = metrics.ResourceSampler(self.root, ram_reader=lambda: next(outcomes))
        for _ in range(3):
            sampler.sample_ram()
        ram = sampler.report()["ram"]
        self.assertEqual(ram["status"], "partial")
        self.assertEqual(ram["sample_count"], 2)
        self.assertEqual(ram["failed_sample_count"], 1)
        self.assertEqual(ram["average_bytes"], 150)

    def test_sampling_intervals_and_roots_validate_before_io(self):
        for interval in (None, True, 0, -1, float("nan"), float("inf")):
            with self.subTest(interval=interval):
                with self.assertRaises(ValueError):
                    metrics.ResourceSampler(self.root, disk_interval=interval)
                with self.assertRaises(ValueError):
                    metrics.ResourceSampler(self.root, sample_interval=interval)
        for roots in ([self.root, self.root], [self.root, self.root / "sub"], []):
            with self.assertRaises(ValueError):
                metrics.ResourceSampler(self.root, disk_roots=roots)
        config = metrics.sampling_config(self.root, .001, .01)
        self.assertEqual(config["ram_interval_seconds"], .01)
        self.assertEqual(config["disk_interval_seconds"], 1)
        self.assertFalse(self.root.exists())

    def test_strict_size_and_missing_root(self):
        self.assertEqual(metrics.directory_bytes(self.root), 0)
        self.root.mkdir()
        (self.root / "data.tex").write_bytes(b"abc")
        (self.root / "nested").mkdir()
        (self.root / "nested" / "x.bib").write_bytes(b"12345")
        self.assertEqual(metrics.directory_bytes(self.root), 8)
        with patch.object(Path, "lstat", side_effect=PermissionError("unreadable")):
            with self.assertRaises(PermissionError):
                metrics.directory_bytes(self.root)

    def test_psutil_reader_includes_descendants(self):
        parent, child = Mock(), Mock()
        parent.children.return_value = [child]
        parent.memory_info.return_value.rss = 100
        child.memory_info.return_value.rss = 30
        fake_psutil = SimpleNamespace(Process=lambda pid: parent, NoSuchProcess=ProcessLookupError)
        with patch.object(metrics, "psutil", fake_psutil):
            read, source, scope = metrics._ram_reader()
            self.assertEqual(read(), 130)
        self.assertEqual(source, "psutil")
        self.assertIn("descendants", scope)

    def test_child_access_denied_does_not_silently_undercount_tree(self):
        parent, child = Mock(), Mock()
        parent.children.return_value = [child]
        parent.memory_info.return_value.rss = 100
        child.memory_info.side_effect = PermissionError()
        with patch.object(metrics, "psutil", SimpleNamespace(Process=lambda pid: parent, NoSuchProcess=ProcessLookupError)):
            sampler = metrics.ResourceSampler(self.root)
            sampler.sample_ram()
        self.assertEqual(sampler.report()["ram"]["status"], "not_collected")

    def test_linux_parent_fallback_is_explicit(self):
        with patch.object(metrics, "psutil", None), patch.object(metrics.sys, "platform", "linux"), \
             patch.object(Path, "read_text", return_value="100 4 1"), \
             patch.object(metrics.os, "sysconf", return_value=4096, create=True):
            read, source, scope = metrics._ram_reader()
            self.assertEqual(read(), 16384)
        self.assertEqual(source, "proc_self_statm")
        self.assertIn("current process only", scope)

    def test_windows_without_backend_is_not_collected(self):
        with patch.object(metrics, "psutil", None), patch.object(metrics.sys, "platform", "win32"):
            sampler = metrics.ResourceSampler(self.root)
            sampler.sample_ram()
        self.assertEqual(sampler.report()["ram"]["status"], "not_collected")
        self.assertIn("backend_missing", sampler.report()["ram"]["failure_reasons"])

    def test_real_windows_or_linux_sampler_smoke_only_on_temp(self):
        self.root.mkdir()
        (self.root / "main.tex").write_bytes(b"fixture")
        sampler = metrics.ResourceSampler(self.root, disk_interval=30)
        sampler.start()
        sampler.stop()
        value = sampler.report()
        self.assertGreaterEqual(value["disk"]["sample_count"], 2)
        self.assertEqual(value["disk"]["final_corpus_bytes"], 7)
        if metrics.psutil is not None:
            self.assertGreater(value["ram"]["average_bytes"], 0)
            self.assertEqual(value["ram"]["source"], "psutil")

    def test_foundation_wall_excludes_final_probe_but_covers_whole_callback(self):
        fake = Mock()
        sample = resource_fixture()
        sample.pop("wall_seconds")
        fake.report.return_value = sample
        def task():
            pass
        with patch.object(benchmark, "ResourceSampler", return_value=fake), \
             patch.object(benchmark.time, "perf_counter", side_effect=[10.0, 17.0]):
            report = benchmark.benchmark_pipeline(
                task, self.root, Path(self.temp.name) / "result.json", verbose=False,
            )
        self.assertEqual(report["metrics_v2"]["wall_seconds"], 7)
        self.assertEqual(report["metrics_v2"]["ram"]["average_bytes"], 20)
        fake.start.assert_called_once()
        fake.stop.assert_called_once()


class IntegratedMetricsTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    sleep = fixtures.PipelineTests.sleep
    open_response = fixtures.PipelineTests.open_response
    store = fixtures.PipelineTests.store
    manifest = fixtures.PipelineTests.manifest
    paper_path = fixtures.PipelineTests.paper_path
    snapshot = fixtures.PipelineTests.snapshot

    def settings(self):
        return dict(
            batch_id="metrics-batch", max_ids=2,
            log_root=Path(self.temp.name) / "logs",
            report_root=Path(self.temp.name) / "reports",
            versions=("v1",), timeout=15, max_attempts=1,
        )

    def engine(self, callback, *, report_path, **kwargs):
        failed = False
        try:
            callback()
        except BaseException:
            failed = True
            raise
        finally:
            value = dict(
                status="failed" if failed else "success",
                metrics_v2=copy.deepcopy(self.resources), ram_source="mock",
            )
            Path(report_path).write_text(json.dumps(value), encoding="utf-8")
        return value

    def run_adapter(self, pairs=None, **options):
        settings = self.settings()
        settings.update(options)
        self.resources = resource_fixture()
        with patch.object(benchmark, "benchmark_pipeline", side_effect=self.engine), \
             patch.object(pipeline.time, "perf_counter", lambda: self.now):
            return runner.run_benchmark([PAIR] if pairs is None else pairs, self.root, **settings)

    def aggregate(self):
        return runner.aggregate_batch(self.root, batch_id="metrics-batch", log_root=self.settings()["log_root"])

    def test_discovery_timing_covers_shared_pacing_and_retry(self):
        self.outcomes = [http_error(503), xml_response(), Response(TEX)]
        self.now = 1.0
        with patch.object(fixtures.client, "_next_request_at", 5.0):
            result = self.run_adapter(max_attempts=2)
        timing = result["timing_metrics"]
        self.assertEqual(timing["discovery_queried"], 1)
        self.assertEqual(timing["discovery_wall_seconds"], 7.0)  # 4s initial pacing + 3s retry.
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual(self.starts, [5, 8, 11])
        self.assertEqual(timing["average_paper_seconds"], 10.0)

    def test_cached_discovery_does_not_add_api_time(self):
        self.outcomes = [xml_response(), Response(TEX)]
        with patch.object(fixtures.client, "_next_request_at", 4.0):
            first = self.run_adapter(experiment_id="first")
        self.assertEqual(first["timing_metrics"]["discovery_wall_seconds"], 4.0)
        source = (self.paper_path() / "v1" / "main.tex").read_bytes()
        second = self.run_adapter(experiment_id="second", resume=True)
        self.assertEqual(second["timing_metrics"]["discovery_queried"], 1)
        self.assertEqual(second["timing_metrics"]["discovery_cached"], 1)
        self.assertEqual(second["timing_metrics"]["discovery_wall_seconds"],
                         first["timing_metrics"]["discovery_wall_seconds"])
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual((self.paper_path() / "v1" / "main.tex").read_bytes(), source)

    def test_two_attempts_sum_wall_weight_ram_and_do_not_sum_disk_snapshots(self):
        self.outcomes = [xml_response(), TimeoutError("fixture")]
        self.run_adapter(experiment_id="first")
        self.outcomes = [Response(TEX)]
        second = self.run_adapter(experiment_id="second", resume=True)
        value = second["benchmark_metrics"]
        self.assertEqual(value["status"], "collected")
        self.assertEqual(value["end_to_end_wall_seconds"], 10)
        self.assertEqual(value["average_ram_bytes"], 20)
        self.assertEqual(value["sampled_peak_ram_bytes"], 30)
        self.assertEqual(value["sampled_peak_disk_bytes"], 40)
        self.assertEqual(value["final_corpus_bytes"], 15)
        self.assertEqual(len(value["attempts"]), 2)
        self.assertEqual(second["data_coverage"]["valid_tex_versions"], 1)
        self.assertFalse(second["publication_complete"]["all_complete"])

    def test_legacy_attempt_without_metrics_is_readable_and_not_backfilled(self):
        self.outcomes = [xml_response(), Response(TEX)]
        settings = self.settings()
        settings.pop("report_root")
        from src.batch import run_batch
        run_batch([PAIR], self.root, **settings)
        directory = settings["log_root"] / "metrics-batch" / "attempts"
        path = next(directory.glob("*.json"))
        value = json.loads(path.read_text())
        value["papers"][0]["result"]["discovery"].pop("wall_seconds")
        value["papers"][0]["result"]["discovery"].pop("timing_status")
        path.write_text(json.dumps(value))
        result = self.aggregate()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["benchmark_metrics"]["status"], "not_collected")
        self.assertIsNone(result["benchmark_metrics"]["end_to_end_wall_seconds"])
        self.assertIsNone(result["history"]["timings"]["discovery_wall_seconds"])

    def test_legacy_plus_measured_attempt_has_only_known_wall_subtotal(self):
        self.outcomes = [xml_response(), Response(TEX)]
        settings = self.settings()
        settings.pop("report_root")
        from src.batch import run_batch
        run_batch([PAIR], self.root, **settings)
        result = self.run_adapter(resume=True)
        value = result["benchmark_metrics"]
        self.assertEqual(value["status"], "partial")
        self.assertIsNone(value["end_to_end_wall_seconds"])
        self.assertEqual(value["known_end_to_end_wall_seconds"], 5)
        self.assertEqual(len(value["missing_attempt_ids"]), 1)

    def test_no_extra_progress_events_are_sent(self):
        self.outcomes = [xml_response(), Response(TEX)]
        events, snapshots = [], []
        def progress(event, summary):
            events.append(event)
            snapshots.append(summary)
        with patch.object(pipeline.time, "perf_counter", lambda: self.now):
            result = pipeline.run_selected(
                [PAIR], self.root, versions=("v1",), timeout=15, max_attempts=1,
                on_progress=progress,
            )
        self.assertEqual(events, ["paper_started", "paper_finished"])
        self.assertEqual(snapshots[0]["papers"][0]["discovery"]["timing_status"], "not_attempted")
        self.assertIsNone(snapshots[0]["papers"][0]["discovery"]["wall_seconds"])
        self.assertEqual(result["papers"][0]["discovery"]["timing_status"], "measured")

    def test_403_429_still_stop_before_second_paper(self):
        for code in (403, 429):
            with self.subTest(code=code):
                self.outcomes = [http_error(code)]
                result = self.run_adapter([PAIR, OTHER], batch_id=f"stop-{code}")
                self.assertEqual(result["batch_status"], "stopped")
                self.assertFalse(result["publication_complete"]["all_complete"])
        self.assertEqual(self.opener.open.call_count, 2)

    def test_interrupted_measured_attempt_has_resources_but_not_complete_data(self):
        self.outcomes = [KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_adapter(experiment_id="interrupt")
        result = json.loads((self.settings()["report_root"] / "interrupt" / "report.json").read_text())
        self.assertEqual(result["execution_status"], "raised")
        self.assertEqual(result["batch_status"], "interrupted")
        self.assertEqual(result["benchmark_metrics"]["end_to_end_wall_seconds"], 5)
        self.assertIsNone(result["timing_metrics"]["discovery_wall_seconds"])
        self.assertFalse(result["publication_complete"]["all_complete"])

    def test_corrupt_measurement_is_reported_without_affecting_checkpoint(self):
        self.outcomes = [xml_response(), Response(TEX)]
        self.run_adapter()
        path = next((self.settings()["log_root"] / "metrics-batch" / "measurements").glob("*.json"))
        path.write_text("{")
        before = self.snapshot()
        result = self.aggregate()
        self.assertEqual(result["benchmark_metrics"]["status"], "not_collected")
        self.assertEqual(len(result["benchmark_metrics"]["invalid_attempt_ids"]), 1)
        self.assertEqual(self.snapshot(), before)

    def test_mismatched_selection_fingerprint_is_not_aggregated(self):
        self.outcomes = [xml_response(), Response(TEX)]
        self.run_adapter()
        path = next((self.settings()["log_root"] / "metrics-batch" / "measurements").glob("*.json"))
        value = json.loads(path.read_text())
        value["selection_fingerprint"] = "not-this-cohort"
        path.write_text(json.dumps(value))
        result = self.aggregate()["benchmark_metrics"]
        self.assertIsNone(result["end_to_end_wall_seconds"])
        self.assertEqual(len(result["invalid_attempt_ids"]), 1)


    def test_unequal_sample_counts_use_sample_weighted_ram_mean(self):
        self.outcomes = [xml_response(), Response(TEX)]
        self.run_adapter(experiment_id="first")
        self.run_adapter(experiment_id="second", resume=True)
        paths = list((self.settings()["log_root"] / "metrics-batch" / "measurements").glob("*.json"))
        value = json.loads(paths[1].read_text())
        value["resources"] = resource_fixture(ram_values=(100, 200), wall=7)
        paths[1].write_text(json.dumps(value))
        summary = self.aggregate()["benchmark_metrics"]
        self.assertEqual(summary["average_ram_bytes"], 72)
        self.assertEqual(summary["sampled_peak_ram_bytes"], 200)
        self.assertEqual(summary["end_to_end_wall_seconds"], 12)

    def test_different_disk_scopes_are_not_merged(self):
        self.outcomes = [xml_response(), Response(TEX)]
        self.run_adapter(experiment_id="first")
        self.run_adapter(experiment_id="second", resume=True)
        path = next((self.settings()["log_root"] / "metrics-batch" / "measurements").glob("*.json"))
        value = json.loads(path.read_text())
        value["resources"]["disk"]["roots"] = ["different-root"]
        path.write_text(json.dumps(value))
        summary = self.aggregate()["benchmark_metrics"]
        self.assertTrue(summary["scope_mismatch"]["disk"])
        self.assertIsNone(summary["sampled_peak_disk_bytes"])
        self.assertEqual(summary["status"], "partial")

    def test_missing_latest_measurement_does_not_reuse_previous_final_size(self):
        self.outcomes = [xml_response(), Response(TEX)]
        self.run_adapter(experiment_id="first")
        second = self.run_adapter(experiment_id="second", resume=True)
        path = self.settings()["log_root"] / "metrics-batch" / "measurements" / (second["attempt_ids"][0] + ".json")
        path.unlink()
        summary = self.aggregate()["benchmark_metrics"]
        self.assertIsNone(summary["final_corpus_bytes"])
        self.assertIsNone(summary["end_to_end_wall_seconds"])

    def test_nonfinite_and_wrong_types_in_resources_are_invalid(self):
        self.outcomes = [xml_response(), Response(TEX)]
        self.run_adapter()
        path = next((self.settings()["log_root"] / "metrics-batch" / "measurements").glob("*.json"))
        original = path.read_bytes()
        for field, invalid in (("wall_seconds", None), ("ram", {}), ("disk", [])):
            with self.subTest(field=field):
                value = json.loads(original)
                value["resources"][field] = invalid
                path.write_text(json.dumps(value))
                summary = self.aggregate()["benchmark_metrics"]
                self.assertIsNone(summary["end_to_end_wall_seconds"])
                self.assertEqual(len(summary["invalid_attempt_ids"]), 1)

    def test_measurement_unknown_fields_and_environment_credentials_are_omitted(self):
        self.outcomes = [xml_response(), Response(TEX)]
        self.run_adapter()
        path = next((self.settings()["log_root"] / "metrics-batch" / "measurements").glob("*.json"))
        value = json.loads(path.read_text())
        value["resources"]["api_key"] = "secret-fixture"
        value["environment"]["API_KEY"] = "secret-fixture"
        path.write_text(json.dumps(value))
        self.assertNotIn("secret-fixture", json.dumps(self.aggregate()["benchmark_metrics"]))

    def test_source_429_stops_and_preserves_completed_paper(self):
        self.outcomes = [xml_response(), Response(TEX), xml_response(OTHER[1]), http_error(429)]
        result = self.run_adapter([PAIR, OTHER])
        self.assertEqual(result["batch_status"], "stopped")
        self.assertEqual(result["stop_reason"]["stage"], "source")
        self.assertEqual(result["data_coverage"]["valid_tex_versions"], 1)
        self.assertFalse(result["publication_complete"]["all_complete"])
        self.assertEqual(self.opener.open.call_count, 4)

    def test_invalid_sampling_config_fails_before_http_or_report_creation(self):
        for options in (dict(disk_interval=0), dict(disk_roots=[]),
                        dict(disk_roots=[self.root, self.root / "nested"])):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.run_adapter(**options)
                self.assertFalse(self.settings()["report_root"].exists())
                self.opener.open.assert_not_called()

    def test_actual_failed_pipeline_records_resources_without_success_claim(self):
        with patch.object(fixtures.client, "query_arxiv", side_effect=RuntimeError("secret-fixture")):
            with self.assertRaises(pipeline.PipelineStopped):
                self.run_adapter(experiment_id="failed")
        path = self.settings()["report_root"] / "failed" / "report.json"
        value = json.loads(path.read_text())
        self.assertEqual(value["execution_status"], "raised")
        self.assertEqual(value["batch_status"], "failed")
        self.assertFalse(value["publication_complete"]["all_complete"])
        self.assertNotIn("secret-fixture", path.read_text())


if __name__ == "__main__":
    unittest.main()
