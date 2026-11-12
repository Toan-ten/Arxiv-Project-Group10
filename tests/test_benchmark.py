import json
import tempfile
import time
import unittest
from pathlib import Path

from src.benchmark import benchmark_pipeline, get_directory_size


class BenchmarkPipelineTests(unittest.TestCase):
    def test_success_writes_report_and_measures_corpus(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            report_path = root / "results" / "benchmark.json"

            def crawler():
                target = corpus / "2508-07872" / "v1"
                target.mkdir(parents=True)
                (target / "main.tex").write_text("x" * 2048, encoding="utf-8")
                time.sleep(0.05)

            report = benchmark_pipeline(
                crawler,
                corpus_output_dir=corpus,
                report_path=report_path,
                sample_interval=0.01,
                verbose=False,
            )

            self.assertEqual(report["status"], "success")
            self.assertIsNone(report["error"])
            self.assertGreaterEqual(report["elapsed_seconds"], 0.05)
            self.assertGreater(report["peak_ram_mb"], 0)
            self.assertGreater(report["disk_corpus_size_mb"], 0)
            self.assertTrue(report_path.exists())

            loaded = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["status"], "success")
            self.assertEqual(loaded["crawler"], "crawler")
            self.assertIsNone(loaded["error"])

    def test_failure_is_reported_and_reraised(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "benchmark.json"

            def crawler():
                raise RuntimeError("boom")

            with self.assertRaisesRegex(RuntimeError, "boom"):
                benchmark_pipeline(
                    crawler,
                    corpus_output_dir=root / "corpus",
                    report_path=report_path,
                    sample_interval=0.01,
                    verbose=False,
                )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["error"]["type"], "RuntimeError")
            self.assertIn("boom", report["error"]["message"])
            self.assertIn("RuntimeError", report["error"]["traceback"])

    def test_missing_directory_has_zero_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                get_directory_size(Path(temporary) / "missing"),
                0,
            )

    def test_non_callable_crawler_is_rejected(self):
        with self.assertRaises(TypeError):
            benchmark_pipeline(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
