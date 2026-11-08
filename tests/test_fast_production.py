"""Tests for high-throughput fast production runner."""
from pathlib import Path
import tempfile
import unittest

from src.fast_production import FastProductionRunner, cohort_pairs


class TestFastProduction(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.local_root = self.root / "local"
        self.drive_root = self.root / "drive"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cohort_pairs_bounds(self):
        c1 = cohort_pairs(1)
        self.assertEqual(len(c1), 100)
        self.assertEqual(c1[0][0], "24120010")
        self.assertEqual(c1[0][1], "2508.07872")
        self.assertEqual(c1[-1][0], "24120010")
        self.assertEqual(c1[-1][1], "2508.07971")

        with self.assertRaises(ValueError):
            cohort_pairs(0)
        with self.assertRaises(ValueError):
            cohort_pairs(201)

    def test_runner_initialization_and_directories(self):
        runner = FastProductionRunner(
            self.local_root,
            self.drive_root,
            scratch_root=self.local_root / "scratch",
            retry_429=True,
            retry_wait_budget=150.0,
            timeout=20.0,
        )
        self.assertTrue(runner.corpus_dir.exists())
        self.assertTrue(runner.logs_dir.exists())
        self.assertTrue(runner.reports_dir.exists())
        self.assertTrue(runner.archives_dir.exists())
        self.assertTrue(runner.scratch_dir.exists())
        self.assertEqual(runner.retry_wait_budget, 150.0)
        self.assertEqual(runner.timeout, 20.0)

    def test_archive_and_restore_cohort(self):
        runner = FastProductionRunner(
            self.local_root,
            self.drive_root,
            scratch_root=self.local_root / "scratch",
            retry_429=False,
        )
        # Create a fake paper directory
        sample_paper = runner.corpus_dir / "2508-07872"
        v1_dir = sample_paper / "v1"
        v1_dir.mkdir(parents=True, exist_ok=True)
        (v1_dir / "main.tex").write_text("Sample LaTeX content", encoding="utf-8")
        (sample_paper / "manifest.json").write_text('{"status": "complete"}', encoding="utf-8")

        # Archive cohort 1
        archive_dest = runner.archives_dir / "cohort_0001_corpus.tar.gz"
        runner._archive_cohort(1, archive_dest)
        self.assertTrue(archive_dest.exists())

        # Simulate fresh Colab session: wipe local_root
        import shutil
        shutil.rmtree(self.local_root)
        self.assertFalse((self.local_root / "corpus").exists())

        # Re-initialize runner and restore cohort 1 from Drive archive
        runner2 = FastProductionRunner(
            self.local_root,
            self.drive_root,
            scratch_root=self.local_root / "scratch",
        )
        restored_count = runner2.restore_cohort(1)
        self.assertEqual(restored_count, 1)
        self.assertTrue((runner2.corpus_dir / "2508-07872/v1/main.tex").exists())
        self.assertEqual(
            (runner2.corpus_dir / "2508-07872/v1/main.tex").read_text(encoding="utf-8"),
            "Sample LaTeX content"
        )

    def test_preload_kaggle_discovery_logs_benchmark(self):
        import json
        snapshot_file = self.root / "snapshot.json"
        snapshot_file.write_text(json.dumps({
            "id": "2508.07872",
            "title": "Test Title",
            "authors_parsed": [["Doe", "Jane", ""]],
            "versions": [{"version": "v1", "created": "Mon, 11 Aug 2025 12:00:00 GMT"}],
        }) + "\n", encoding="utf-8")

        runner = FastProductionRunner(
            self.local_root,
            self.drive_root,
            scratch_root=self.local_root / "scratch",
        )
        res = runner.preload_kaggle_discovery(snapshot_file, [1])
        self.assertEqual(res["found_papers"], 1)
        self.assertIn("elapsed_seconds", res)
        self.assertTrue(Path(res["report_path"]).exists())


if __name__ == "__main__":
    unittest.main()
