"""Unit tests for safe legacy HTTP 404 withdrawal reconciliation."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.storage import CorpusStorage, TERMINAL
from src.storage_integrity import reconcile_withdrawn_version_evidence


class TestReconcileWithdrawnVersionEvidence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = CorpusStorage(self.root)
        self.mssv = "24120260"
        self.arxiv_id = "2501.02724"

        self.store.initialize_paper(self.mssv, self.arxiv_id)
        self.paper_dir = self.store.paper_path(self.mssv, self.arxiv_id)

        # Setup v1 as success with real TeX file
        self.v1_dir = self.paper_dir / "v1"
        self.v1_dir.mkdir(parents=True, exist_ok=True)
        self.v1_tex = b"\\documentclass{article}\\begin{document}Schiffer proof\\end{document}"
        (self.v1_dir / "main.tex").write_bytes(self.v1_tex)
        self.v1_sha = hashlib.sha256(self.v1_tex).hexdigest()

        # Write initial manifest matching legacy Drive state:
        # v1: success, v2: http_error (404)
        self.manifest_data = {
            "contract_version": 2,
            "arxiv_id": self.arxiv_id,
            "mssv": self.mssv,
            "run_id": "legacy-run-001",
            "status": "partial",
            "discovery_status": "found",
            "version_status": "resolved",
            "metadata_status": "partial",
            "references_status": "not_attempted",
            "versions": {
                "v1": {
                    "versioned_id": f"{self.arxiv_id}v1",
                    "status": "success",
                    "source_status": "success",
                    "http_status": 200,
                    "content_type": "application/gzip",
                    "source_url": f"https://export.arxiv.org/src/{self.arxiv_id}v1",
                    "archive_bytes": 30619,
                    "archive_sha256": None,
                    "files": ["main.tex"],
                    "file_checksums": {"main.tex": self.v1_sha},
                    "kept_bytes": len(self.v1_tex),
                    "extracted_bytes": None,
                    "removed_files": None,
                    "attempts": 1,
                    "run_id": "legacy-run-001",
                    "completed_at": "2026-11-08T22:25:25.956080Z",
                    "error": None,
                },
                "v2": {
                    "versioned_id": f"{self.arxiv_id}v2",
                    "status": "http_error",
                    "source_status": "http_error",
                    "http_status": 404,
                    "content_type": None,
                    "source_url": f"https://export.arxiv.org/src/{self.arxiv_id}v2",
                    "archive_bytes": None,
                    "archive_sha256": None,
                    "files": [],
                    "file_checksums": {},
                    "kept_bytes": None,
                    "extracted_bytes": None,
                    "removed_files": None,
                    "attempts": 1,
                    "run_id": "legacy-run-001",
                    "completed_at": None,
                    "error": {
                        "stage": "source",
                        "type": "http_error",
                        "message": "HTTP Error 404: Not Found",
                    },
                },
            },
            "updated_at": "2026-11-08T22:25:29.270895Z",
        }
        with open(self.paper_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(self.manifest_data, f, indent=2)

        # Write initial metadata (legacy style: without comments/abstract in snapshot)
        self.metadata_data = {
            "contract_version": 2,
            "arxiv_id": self.arxiv_id,
            "mssv": self.mssv,
            "status": "partial",
            "title": "Confirmed answer to the Schiffer conjecture and the Berenstein conjecture",
            "authors": ["Guowei Dai"],
            "submission_date": "2025-01-06",
            "revised_dates": ["2025-01-15"],
            "venue": None,
            "version_status": "resolved",
            "versions": [
                {"version": "v1", "versioned_id": f"{self.arxiv_id}v1", "date": "2025-01-06", "date_status": "verified"},
                {"version": "v2", "versioned_id": f"{self.arxiv_id}v2", "date": "2025-01-15", "date_status": "verified"},
            ],
            "missing_fields": [],
            "provenance": {
                "arxiv": {
                    "query_identifier": self.arxiv_id,
                    "retrieved_at": "2026-11-08T22:14:29.328773Z",
                    "snapshot": {
                        "mssv": self.mssv,
                        "arxiv_id": self.arxiv_id,
                        "status": "found",
                        "version_status": "resolved",
                        "version_ids": [f"{self.arxiv_id}v1", f"{self.arxiv_id}v2"],
                        "latest_version_id": f"{self.arxiv_id}v2",
                        "title": "Confirmed answer to the Schiffer conjecture and the Berenstein conjecture",
                        "authors": ["Guowei Dai"],
                        "published": "2025-01-06",
                        "updated": "2025-01-15",
                        "revised_dates": ["2025-01-15"],
                        "missing_fields": [],
                    },
                },
                "semantic_scholar": None,
                "version_history": None,
            },
        }
        with open(self.paper_dir / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(self.metadata_data, f, indent=2)

        # References
        self.references_data = {
            "contract_version": 2,
            "arxiv_id": self.arxiv_id,
            "mssv": self.mssv,
            "status": "not_attempted",
            "provenance": {
                "provider": "semantic_scholar",
                "query_identifier": None,
                "s2_paper_id": None,
                "retrieved_at": None,
            },
            "coverage": {
                "complete": False,
                "continuation": None,
                "reported_total": None,
                "retrieved_count": 0,
            },
            "items": [],
        }
        with open(self.paper_dir / "references.json", "w", encoding="utf-8") as f:
            json.dump(self.references_data, f, indent=2)

        # Official withdrawal evidence for 2501.02724v2
        self.official_evidence = {
            "source": "arxiv_official_abstract",
            "source_identifier": self.arxiv_id,
            "retrieved_at": "2026-11-10T00:00:00Z",
            "arxiv_id": self.arxiv_id,
            "version": "v2",
            "versioned_id": f"{self.arxiv_id}v2",
            "official_url": f"https://arxiv.org/abs/{self.arxiv_id}v2",
            "withdrawal_reason": "Withdrawn by the author: insufficient proof",
            "comments": "The proof of constant breadth of domain is insufficient. After the modification, I will upload it again.",
            "abstract": "This paper has been withdrawn by the author due to an insufficient proof of constant breadth of domain.",
            "is_withdrawn": True,
            "version_ids": [f"{self.arxiv_id}v1", f"{self.arxiv_id}v2"],
            "latest_version_id": f"{self.arxiv_id}v2",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_dry_run_with_official_evidence_makes_no_disk_changes(self):
        manifest_before = (self.paper_dir / "manifest.json").read_bytes()
        res = reconcile_withdrawn_version_evidence(
            self.store, self.mssv, self.arxiv_id, "v2",
            evidence=self.official_evidence,
            dry_run=True,
        )
        self.assertEqual(res["status"], "ready_to_apply")
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["target_status"], "withdrawn_version")
        self.assertEqual(res["current_status"], "http_error")
        # Ensure manifest file was NOT touched
        manifest_after = (self.paper_dir / "manifest.json").read_bytes()
        self.assertEqual(manifest_before, manifest_after)

    def test_apply_with_official_evidence_reclassifies_v2_to_withdrawn(self):
        res = reconcile_withdrawn_version_evidence(
            self.store, self.mssv, self.arxiv_id, "v2",
            evidence=self.official_evidence,
            dry_run=False,
        )
        self.assertEqual(res["status"], "reconciled")
        self.assertFalse(res["dry_run"])

        # Check updated manifest
        manifest = self.store.read_manifest(self.mssv, self.arxiv_id)
        v2 = manifest["versions"]["v2"]
        self.assertEqual(v2["status"], "withdrawn_version")
        self.assertEqual(v2["source_status"], "withdrawn_version")
        self.assertIsNone(v2["error"])
        self.assertEqual(v2["files"], [])
        self.assertEqual(v2["file_checksums"], {})
        self.assertEqual(v2["kept_bytes"], 0)
        self.assertIsNotNone(v2["completed_at"])
        self.assertIn("withdrawal_evidence", v2)
        self.assertEqual(v2["withdrawal_evidence"]["classification"], "withdrawn_version")

        # Paper status should be partial because metadata and references are not yet complete
        self.assertEqual(manifest["status"], "partial")

        # But all versions must be in TERMINAL, meaning source is 100% complete
        all_terminal = all(v["status"] in TERMINAL for v in manifest["versions"].values())
        self.assertTrue(all_terminal)



        # Ensure v1 remains 100% untouched
        v1 = manifest["versions"]["v1"]
        self.assertEqual(v1["status"], "success")
        self.assertEqual(v1["files"], ["main.tex"])
        self.assertEqual(v1["file_checksums"]["main.tex"], self.v1_sha)
        self.assertEqual((self.v1_dir / "main.tex").read_bytes(), self.v1_tex)

    def test_apply_without_evidence_refuses_to_reclassify(self):
        clean_evidence = {
            "source": "test",
            "source_identifier": self.arxiv_id,
            "retrieved_at": "2026-11-10T00:00:00Z",
            "arxiv_id": self.arxiv_id,
            "version": "v2",
            "comments": "Normal mathematical comments about elliptic boundary value problems.",
            "abstract": "Normal abstract about boundary values of elliptic problems.",
            "is_withdrawn": False,
            "version_ids": [f"{self.arxiv_id}v1", f"{self.arxiv_id}v2"],
            "latest_version_id": f"{self.arxiv_id}v2",
        }
        res = reconcile_withdrawn_version_evidence(
            self.store, self.mssv, self.arxiv_id, "v2",
            evidence=clean_evidence,
            dry_run=False,
        )
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["classification"], "http_error_404_unverified")
        self.assertFalse(res["is_terminal"])

        # Manifest must remain http_error
        manifest = self.store.read_manifest(self.mssv, self.arxiv_id)
        self.assertEqual(manifest["versions"]["v2"]["status"], "http_error")
        self.assertEqual(manifest["status"], "partial")

    def test_apply_is_idempotent_on_repeated_runs(self):
        # First apply
        res1 = reconcile_withdrawn_version_evidence(
            self.store, self.mssv, self.arxiv_id, "v2",
            evidence=self.official_evidence,
            dry_run=False,
        )
        self.assertEqual(res1["status"], "reconciled")

        # Second apply should detect version is already terminal and skip safely
        res2 = reconcile_withdrawn_version_evidence(
            self.store, self.mssv, self.arxiv_id, "v2",
            evidence=self.official_evidence,
            dry_run=False,
        )
        self.assertEqual(res2["status"], "skipped")
        self.assertIn("already terminal", res2["reason"])

    def test_apply_blocks_if_v1_checksum_mismatches(self):
        # Tamper v1 file
        (self.v1_dir / "main.tex").write_bytes(b"tampered content")

        with self.assertRaises(Exception) as ctx:
            reconcile_withdrawn_version_evidence(
                self.store, self.mssv, self.arxiv_id, "v2",
                evidence=self.official_evidence,
                dry_run=False,
            )
        self.assertIn("v1", str(ctx.exception).lower())

        # Ensure manifest was NOT changed to withdrawn
        manifest = self.store.read_manifest(self.mssv, self.arxiv_id)
        self.assertEqual(manifest["versions"]["v2"]["status"], "http_error")

    def test_apply_blocks_if_destination_directory_exists(self):
        v2_dir = self.paper_dir / "v2"
        v2_dir.mkdir(parents=True, exist_ok=True)
        (v2_dir / "file.txt").write_text("some file")

        res = reconcile_withdrawn_version_evidence(
            self.store, self.mssv, self.arxiv_id, "v2",
            evidence=self.official_evidence,
            dry_run=False,
        )
        self.assertEqual(res["status"], "blocked")
        self.assertIn("destination", res["reason"].lower())

    def test_version_not_in_version_ids_is_not_marked_withdrawn(self):
        """Scenario 3: 404 for a version absent from official version_ids is version_not_found, not withdrawn."""
        evidence_absent = {
            "source": "arxiv_metadata",
            "source_identifier": self.arxiv_id,
            "retrieved_at": "2026-11-10T00:00:00Z",
            "arxiv_id": self.arxiv_id,
            "version": "v2",
            "is_withdrawn": False,
            "version_ids": [f"{self.arxiv_id}v1"],  # v2 absent!
            "latest_version_id": f"{self.arxiv_id}v1",
        }
        res = reconcile_withdrawn_version_evidence(
            self.store, self.mssv, self.arxiv_id, "v2",
            evidence=evidence_absent,
            dry_run=False,
        )
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["classification"], "version_not_found")
        # Ensure it was not marked as withdrawn_version
        manifest = self.store.read_manifest(self.mssv, self.arxiv_id)
        self.assertNotEqual(manifest["versions"]["v2"]["status"], "withdrawn_version")

    def test_missing_or_malformed_metadata_fails_closed(self):
        """Scenario 5: Malformed or missing discovery metadata fails closed without crashing."""
        res = reconcile_withdrawn_version_evidence(
            self.store, self.mssv, self.arxiv_id, "v2",
            evidence={},
            dry_run=False,
        )
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["classification"], "http_error_404_unverified")
        self.assertFalse(res["is_terminal"])

    def test_http_500_or_429_blocked_from_withdrawal_reconcile(self):
        """Scenario 13: HTTP 500 or 429 errors cannot be reconciled as withdrawn."""
        # Set v2 status to server_error (500)
        manifest = self.store.read_manifest(self.mssv, self.arxiv_id)
        manifest["versions"]["v2"]["status"] = "server_error"
        manifest["versions"]["v2"]["http_status"] = 500
        with open(self.paper_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        res = reconcile_withdrawn_version_evidence(
            self.store, self.mssv, self.arxiv_id, "v2",
            evidence=self.official_evidence,
            dry_run=False,
        )
        self.assertEqual(res["status"], "blocked")
        self.assertIn("not an unverified http 404", res["reason"].lower())

    def test_no_phantom_source_files_for_withdrawn(self):
        """Scenario 10: Reconciling withdrawn version leaves zero files and kept_bytes 0."""
        reconcile_withdrawn_version_evidence(
            self.store, self.mssv, self.arxiv_id, "v2",
            evidence=self.official_evidence,
            dry_run=False,
        )
        v2_dir = self.paper_dir / "v2"
        self.assertFalse(v2_dir.exists(), "Withdrawn version should not have directory on disk")
        manifest = self.store.read_manifest(self.mssv, self.arxiv_id)
        self.assertEqual(manifest["versions"]["v2"]["files"], [])
        self.assertEqual(manifest["versions"]["v2"]["kept_bytes"], 0)

    def test_store_resume_requires_exact_page_after_metadata_withdrawal_hint(self):
        """Paper-wide metadata remains a hint; resume schedules the exact version page check."""
        meta = self.store.read_metadata(self.mssv, self.arxiv_id)
        snap = meta["provenance"]["arxiv"]["snapshot"]
        snap["comments"] = self.official_evidence["comments"]
        snap["is_withdrawn"] = True
        self.store.save_metadata(self.mssv, self.arxiv_id, meta)

        plan = self.store.resume(self.mssv, self.arxiv_id)
        self.assertIn("v1", plan["skip"])
        self.assertIn("v2", plan["verify_withdrawal"])
        self.assertNotIn("v2", plan["blocked"])
        self.assertEqual(plan["manifest"]["versions"]["v2"]["status"], "http_error")

    def test_resume_reclassifies_legacy_unverified_withdrawal_without_integrity_failure(self):
        """A stale broad withdrawal claim is preserved, downgraded, and queued for page verification."""
        manifest = self.store.read_manifest(self.mssv, self.arxiv_id)
        record = manifest["versions"]["v2"]
        stale_evidence = {
            "classification": "withdrawn_version",
            "is_terminal": True,
            "evidence_source": "arxiv_metadata",
            "evidence_timestamp": "2026-11-10T09:35:12.828146+00:00",
            "original_http_status": 404,
            "reason": "The proof of constant breadth of domain is insufficient.",
            "details": {
                "comments": "The proof of constant breadth of domain is insufficient. After the modification, I will upload it again",
                "title": "",
                "abstract": "",
                "withdrawal_reason": "",
            },
        }
        record.update(
            status="withdrawn_version",
            source_status="withdrawn_version",
            completed_at="2026-11-10T09:35:12.828146+00:00",
            kept_bytes=0,
            withdrawal_evidence=stale_evidence,
        )
        with (self.paper_dir / "manifest.json").open("w", encoding="utf-8") as stream:
            json.dump(manifest, stream)

        plan = self.store.resume(self.mssv, self.arxiv_id)

        updated = plan["manifest"]["versions"]["v2"]
        self.assertIn("v2", plan["verify_withdrawal"])
        self.assertEqual(updated["status"], "http_error_404_unverified")
        self.assertEqual(updated["source_status"], "http_error_404_unverified")
        self.assertEqual(updated["withdrawal_evidence_history"][0]["evidence"], stale_evidence)
        self.assertEqual(
            updated["withdrawal_evidence_history"][0]["review_result"],
            "rejected_missing_version_scoped_proof",
        )
        self.assertNotIn("withdrawal_evidence", updated)
        self.assertFalse(self.store.audit_paper(self.mssv, self.arxiv_id).has_integrity_failure)

    def test_store_resume_schedules_unverified_404_for_exact_page_check(self):
        """An unresolved 404 is checked on its exact official version page."""
        plan = self.store.resume(self.mssv, self.arxiv_id)
        self.assertIn("v1", plan["skip"])
        self.assertIn("v2", plan["verify_withdrawal"])
        self.assertNotIn("v2", plan["blocked"])
        self.assertNotIn("v2", plan["retry"])
        self.assertEqual(plan["manifest"]["versions"]["v2"]["status"], "http_error")


if __name__ == "__main__":
    unittest.main()
