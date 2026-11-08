"""Tests for Kaggle snapshot ingestion and pre-cached discovery."""
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from src.kaggle_discovery import build_discovery_snapshot, ingest_kaggle_snapshot, _parse_iso_date
from src.storage import CorpusStorage


class TestKaggleDiscovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = CorpusStorage(self.root / "corpus")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_iso_date(self):
        self.assertEqual(_parse_iso_date("2025-08-15"), "2025-08-15")
        self.assertEqual(_parse_iso_date("2025-08-15T12:00:00Z"), "2025-08-15")
        self.assertEqual(_parse_iso_date("Mon, 2 Apr 2007 19:18:42 GMT"), "2007-04-02")
        self.assertIsNone(_parse_iso_date("invalid-date"))
        self.assertIsNone(_parse_iso_date(None))

    def test_build_discovery_snapshot(self):
        record = {
            "id": "2508.07872",
            "title": "A Sample Paper Title\nWith Newline",
            "authors": "Author One, Author Two",
            "versions": [
                {"version": "v1", "created": "Mon, 15 Aug 2025 10:00:00 GMT"},
                {"version": "v2", "created": "Wed, 17 Aug 2025 12:00:00 GMT"},
            ],
            "update_date": "2025-08-18",
        }
        snapshot = build_discovery_snapshot("24120010", "2508.07872", record)
        self.assertEqual(snapshot["mssv"], "24120010")
        self.assertEqual(snapshot["arxiv_id"], "2508.07872")
        self.assertEqual(snapshot["status"], "found")
        self.assertEqual(snapshot["version_status"], "resolved")
        self.assertEqual(snapshot["version_ids"], ["2508.07872v1", "2508.07872v2"])
        self.assertEqual(snapshot["latest_version_id"], "2508.07872v2")
        self.assertEqual(snapshot["title"], "A Sample Paper Title With Newline")
        self.assertEqual(snapshot["authors"], ["Author One", "Author Two"])
        self.assertEqual(snapshot["published"], "2025-08-15")
        self.assertEqual(snapshot["updated"], "2025-08-17")
        self.assertEqual(snapshot["revised_dates"], ["2025-08-17"])

        # Test recording into storage
        manifest = self.store.record_discovery(snapshot)
        self.assertEqual(manifest["discovery_status"], "found")
        self.assertEqual(manifest["version_status"], "resolved")
        self.assertEqual(set(manifest["versions"].keys()), {"v1", "v2"})

    def test_build_discovery_snapshot_with_multiple_revised_dates(self):
        record = {
            "id": "2508.07872",
            "title": "Multi-version Paper",
            "authors": "Author A, Author B",
            "versions": [
                {"version": "v1", "created": "Mon, 15 Aug 2025 10:00:00 GMT"},
                {"version": "v2", "created": "Wed, 17 Aug 2025 12:00:00 GMT"},
                {"version": "v3", "created": "Fri, 19 Aug 2025 14:00:00 GMT"},
            ],
            "update_date": "2025-08-20",
        }
        snapshot = build_discovery_snapshot("24120010", "2508.07872", record)
        self.assertEqual(snapshot["published"], "2025-08-15")
        self.assertEqual(snapshot["revised_dates"], ["2025-08-17", "2025-08-19"])

        manifest = self.store.record_discovery(snapshot)
        meta = self.store.read_metadata("24120010", "2508.07872")
        meta["revised_dates"] = snapshot["revised_dates"]
        self.store.save_metadata("24120010", "2508.07872", meta)

        saved_meta = self.store.read_metadata("24120010", "2508.07872")
        self.assertEqual(saved_meta["submission_date"], "2025-08-15")
        self.assertEqual(saved_meta["revised_dates"], ["2025-08-17", "2025-08-19"])

    def test_ingest_kaggle_snapshot_streaming(self):
        sample_records = [
            {
                "id": "2508.07872",
                "title": "Paper One",
                "authors": "Alice, Bob",
                "versions": [{"version": "v1", "created": "2025-08-01"}],
            },
            {
                "id": "2508.07912",
                "title": "Paper Two",
                "authors": "Charlie",
                "versions": [
                    {"version": "v1", "created": "2025-08-02"},
                    {"version": "v2", "created": "2025-08-03"},
                ],
            },
            {
                "id": "9999.99999",
                "title": "Unrelated Paper",
                "authors": "Dave",
                "versions": [{"version": "v1", "created": "2025-08-04"}],
            },
        ]
        snapshot_file = self.root / "arxiv-metadata-sample.json"
        with open(snapshot_file, "w", encoding="utf-8") as f:
            for rec in sample_records:
                f.write(json.dumps(rec) + "\n")

        pairs = [("24120010", "2508.07872"), ("24120010", "2508.07912"), ("24120010", "2508.99999")]
        summary = ingest_kaggle_snapshot(snapshot_file, self.store, pairs)

        self.assertEqual(summary["target_papers"], 3)
        self.assertEqual(summary["found_papers"], 2)
        self.assertEqual(summary["found_versions"], 3)
        self.assertEqual(summary["missing_papers"], ["2508.99999"])

        # Check storage manifests
        man1 = self.store.read_manifest("24120010", "2508.07872")
        self.assertEqual(man1["discovery_status"], "found")
        self.assertEqual(list(man1["versions"].keys()), ["v1"])

        man2 = self.store.read_manifest("24120010", "2508.07912")
        self.assertEqual(man2["discovery_status"], "found")
        self.assertEqual(list(man2["versions"].keys()), ["v1", "v2"])
        meta2 = self.store.read_metadata("24120010", "2508.07912")
        self.assertEqual(meta2["versions"][0]["date"], "2025-08-02")
        self.assertEqual(meta2["versions"][0]["date_status"], "verified")
        self.assertEqual(meta2["versions"][1]["date"], "2025-08-03")
        self.assertEqual(meta2["versions"][1]["date_status"], "verified")


if __name__ == "__main__":
    unittest.main()
