"""Storage tests use only temporary fixtures, never real API/source data."""
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import arxiv_client, arxiv_source, storage
from src.arxiv_discovery import parse_arxiv_response
from src.storage import CorpusStorage, IntegrityError, StorageError


MSSV = "24120232"
ARXIV_ID = "2502.08235"
DATE = "2025-02-12T09:00:00Z"
TEX = b"\\documentclass{article}\r\n\\include{chapters/method}\xff\r\n"
BIB = b"@article{fixture, title={Offline only}}\r\n"


def discovery(versions=3, arxiv_id=ARXIV_ID):
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        f'<id>http://arxiv.org/abs/{arxiv_id}v{versions}</id>'
        '<title>Offline fixture</title><author><name>Test Author</name></author>'
        f'<published>{DATE}</published><updated>{DATE}</updated>'
        '</entry></feed>'
    )
    return parse_arxiv_response(xml, MSSV, arxiv_id)


def archive_bytes(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, body in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "corpus"
        self.store = CorpusStorage(self.root, run_id="offline-fixture")
        self.store.initialize_paper(MSSV, ARXIV_ID)

    def discover(self, versions=3):
        return self.store.record_discovery(discovery(versions))

    def result(self, version, status, files=None, error=None, **fields):
        return dict(
            mssv=MSSV, arxiv_id=ARXIV_ID, versioned_id=ARXIV_ID + version,
            status=status, files=[] if files is None else files, error=error, **fields,
        )

    def put_source(self, version="v1", entries=None):
        if not self.store.read_manifest(MSSV, ARXIV_ID)["versions"]:
            self.discover()
        destination = self.store.begin_version(MSSV, ARXIV_ID, version)
        entries = {"main.tex": TEX, "bib/refs.bib": BIB} if entries is None else entries
        payload = Path(self.temp.name) / (version + ".tar.gz")
        payload.write_bytes(archive_bytes(entries))
        extracted = arxiv_source.extract_source(payload, destination)
        self.store.record_source_result(self.result(version, **extracted))
        return destination

    def complete_metadata(self):
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        value.update(status="complete", missing_fields=[], revised_dates=[])
        value["provenance"]["semantic_scholar"] = {
            "query_identifier": "ARXIV:" + ARXIV_ID, "retrieved_at": DATE,
            "snapshot": {"title": "Offline fixture"},
        }
        return value

    def complete_references(self, items=None):
        value = self.store.read_references(MSSV, ARXIV_ID)
        value.update(status="complete", items=[] if items is None else items)
        value["provenance"].update(query_identifier="ARXIV:" + ARXIV_ID,
                                   retrieved_at=DATE)
        value["coverage"].update(
            complete=True, continuation=None, reported_total=len(value["items"]),
            retrieved_count=len(value["items"]),
        )
        return value

    def item(self, identifier):
        return dict(
            arxiv_id=identifier, title=None, authors=None, submission_date=None,
            publication_date=None, year=None, venue=None, s2_paper_id=None,
            missing_fields=["title"], raw={"externalIds": {"ArXiv": identifier}},
        )

    def test_initialize_preserves_identity_and_unknown_metadata(self):
        meta = self.store.read_metadata(MSSV, ARXIV_ID)
        self.assertEqual((meta["mssv"], meta["arxiv_id"]), (MSSV, ARXIV_ID))
        self.assertEqual(meta["contract_version"], 2)
        self.assertIsNone(meta["title"])
        self.assertIsNone(meta["revised_dates"])
        refs = self.store.read_references(MSSV, ARXIV_ID)
        self.assertEqual(refs["status"], "not_attempted")
        self.assertFalse(refs["coverage"]["complete"])
        self.assertEqual(refs["items"], [])
        self.assertFalse((self.root / "2502-08235" / "v1").exists())

    def test_initialize_again_does_not_reset_checkpoint(self):
        self.put_source()
        old = {p.name: p.read_bytes() for p in self.store.paper_path(MSSV, ARXIV_ID).glob("*.json")}
        self.store.initialize_paper(MSSV, ARXIV_ID)
        self.assertEqual(old, {p.name: p.read_bytes() for p in self.store.paper_path(MSSV, ARXIV_ID).glob("*.json")})

    def test_discovery_saves_all_versions_without_making_directories(self):
        manifest = self.discover()
        self.assertEqual(set(manifest["versions"]), {"v1", "v2", "v3"})
        self.assertEqual(manifest["status"], "partial")
        self.assertTrue(all(v["status"] == "pending" for v in manifest["versions"].values()))
        self.assertFalse(self.store.version_destination(MSSV, ARXIV_ID, "v1").exists())
        meta = self.store.read_metadata(MSSV, ARXIV_ID)
        self.assertEqual(meta["submission_date"], DATE)
        self.assertIsNone(meta["revised_dates"])
        self.assertIsNone(meta["versions"][1]["date"])
        self.assertIsNone(meta["provenance"]["semantic_scholar"])

    def test_unresolved_discovery_does_not_invent_versions(self):
        result = discovery()
        result.update(version_status="needs_verification", version_ids=[], latest_version_id=None)
        manifest = self.store.record_discovery(result)
        self.assertEqual(manifest["versions"], {})
        with self.assertRaises(StorageError):
            self.store.begin_version(MSSV, ARXIV_ID, "v1")

    def test_discovery_cannot_remove_known_versions(self):
        self.discover(3)
        with self.assertRaises(StorageError):
            self.store.record_discovery(discovery(1))
        self.assertEqual(len(self.store.read_manifest(MSSV, ARXIV_ID)["versions"]), 3)

    def test_discovery_cannot_replace_found_with_not_found(self):
        self.discover()
        with self.assertRaises(StorageError):
            self.store.record_discovery(dict(mssv=MSSV, arxiv_id=ARXIV_ID, status="not_found"))

    def test_not_found_does_not_mean_empty_references(self):
        manifest = self.store.record_discovery(
            dict(mssv=MSSV, arxiv_id=ARXIV_ID, status="not_found"))
        self.assertEqual(manifest["status"], "not_found")
        self.assertEqual(self.store.read_references(MSSV, ARXIV_ID)["status"], "not_attempted")

    def test_metadata_round_trip_keeps_null_and_utf8(self):
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        value.update(status="partial", title="Tài liệu giả lập", authors=["Nguyễn Văn Toàn"])
        self.store.save_metadata(MSSV, ARXIV_ID, value)
        self.assertEqual(self.store.read_metadata(MSSV, ARXIV_ID), value)
        self.assertIn("Tài liệu".encode(), self.store.paper_path(MSSV, ARXIV_ID).joinpath("metadata.json").read_bytes())

    def test_complete_metadata_needs_real_stage_evidence(self):
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        value["status"] = "complete"
        with self.assertRaises(StorageError):
            self.store.save_metadata(MSSV, ARXIV_ID, value)

    def test_wrong_identity_and_contract_are_rejected(self):
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        for field, wrong in [("mssv", "24120010"), ("arxiv_id", "2502.08236"),
                             ("contract_version", 1)]:
            changed = {**value, field: wrong}
            with self.subTest(field=field), self.assertRaises(StorageError):
                self.store.save_metadata(MSSV, ARXIV_ID, changed)

    def test_wrong_owner_cannot_initialize_paper(self):
        with self.assertRaises(StorageError):
            self.store.initialize_paper("24120010", ARXIV_ID)

    def test_references_keep_unknown_and_legacy_ids_without_filtering(self):
        items = [self.item(None), self.item("hep-th/9901001"), self.item("0704.0001")]
        value = self.complete_references(items)
        self.store.save_references(MSSV, ARXIV_ID, value)
        self.assertEqual(self.store.read_references(MSSV, ARXIV_ID)["items"], items)
        self.assertIsNone(self.store.read_references(MSSV, ARXIV_ID)["items"][0]["arxiv_id"])

    def test_reference_coverage_must_match_count_and_pagination(self):
        for change in [dict(retrieved_count=1), dict(complete=False),
                       dict(continuation={"offset": 10}), dict(reported_total=2)]:
            value = self.complete_references()
            value["coverage"].update(change)
            with self.subTest(change=change), self.assertRaises(StorageError):
                self.store.save_references(MSSV, ARXIV_ID, value)

    def test_empty_references_are_complete_only_with_provenance(self):
        value = self.store.read_references(MSSV, ARXIV_ID)
        value["status"] = "complete"
        value["coverage"]["complete"] = True
        with self.assertRaises(StorageError):
            self.store.save_references(MSSV, ARXIV_ID, value)
        good = self.complete_references()
        self.store.save_references(MSSV, ARXIV_ID, good)
        self.assertEqual(self.store.read_references(MSSV, ARXIV_ID), good)

    def test_completed_references_cannot_be_downgraded(self):
        value = self.complete_references([self.item(None)])
        self.store.save_references(MSSV, ARXIV_ID, value)
        value.update(status="failed")
        value["coverage"]["complete"] = False
        with self.assertRaises(StorageError):
            self.store.save_references(MSSV, ARXIV_ID, value)

    def test_partial_reference_checkpoint_cannot_drop_saved_items(self):
        value = self.store.read_references(MSSV, ARXIV_ID)
        value.update(status="partial", items=[self.item(None)])
        value["coverage"]["retrieved_count"] = 1
        self.store.save_references(MSSV, ARXIV_ID, value)
        changed = self.store.read_references(MSSV, ARXIV_ID)
        changed["items"] = []
        changed["coverage"]["retrieved_count"] = 0
        with self.assertRaises(StorageError):
            self.store.save_references(MSSV, ARXIV_ID, changed)
        self.assertEqual(self.store.read_references(MSSV, ARXIV_ID)["items"], value["items"])

    def test_rediscovery_keeps_previously_known_metadata(self):
        self.discover()
        result = discovery()
        result.update(title=None, authors=[], published=None,
                      missing_fields=["title", "authors", "published"])
        self.store.record_discovery(result)
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        self.assertEqual(value["title"], "Offline fixture")
        self.assertEqual(value["authors"], ["Test Author"])
        self.assertEqual(value["submission_date"], DATE)

    def test_metadata_dates_must_agree_before_complete(self):
        self.discover(1)
        value = self.complete_metadata()
        value["submission_date"] = "2025-03-01"
        with self.assertRaises(StorageError):
            self.store.save_metadata(MSSV, ARXIV_ID, value)

    def test_error_snapshot_cannot_erase_known_partial_metadata(self):
        self.discover()
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        value.update(status="failed", title=None, authors=None)
        with self.assertRaises(StorageError):
            self.store.save_metadata(MSSV, ARXIV_ID, value)
        self.assertEqual(self.store.read_metadata(MSSV, ARXIV_ID)["title"], "Offline fixture")

    def test_rediscovery_does_not_erase_verified_version_dates(self):
        self.discover(1)
        result = discovery(1)
        result.update(published=None, missing_fields=["published"])
        self.store.record_discovery(result)
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        self.assertEqual(value["versions"][0]["date"], DATE)
        self.assertEqual(value["versions"][0]["date_status"], "verified")

    def test_interrupted_reference_stage_is_recovered_without_fake_items(self):
        value = self.store.read_references(MSSV, ARXIV_ID)
        value["status"] = "in_progress"
        self.store.save_references(MSSV, ARXIV_ID, value)
        manifest = CorpusStorage(self.root).resume(MSSV, ARXIV_ID)["manifest"]
        self.assertEqual(manifest["references_status"], "interrupted")
        self.assertEqual(self.store.read_references(MSSV, ARXIV_ID)["items"], [])

    def test_multiple_versions_preserve_bytes_paths_and_checksums(self):
        first = self.put_source("v1")
        second = self.put_source("v2", {"chapters/method.tex": b"Version 2\x00\xff"})
        self.assertEqual((first / "main.tex").read_bytes(), TEX)
        self.assertEqual((first / "bib/refs.bib").read_bytes(), BIB)
        self.assertEqual((second / "chapters/method.tex").read_bytes(), b"Version 2\x00\xff")
        manifest = self.store.read_manifest(MSSV, ARXIV_ID)
        self.assertEqual(manifest["versions"]["v1"]["files"], ["bib/refs.bib", "main.tex"])
        self.assertEqual(manifest["versions"]["v3"]["status"], "pending")
        self.assertEqual(len(manifest["versions"]["v1"]["file_checksums"]["main.tex"]), 64)

    def test_completed_version_is_skipped_and_repeat_result_is_idempotent(self):
        destination = self.put_source()
        before = (destination / "main.tex").read_bytes()
        manifest = self.store.read_manifest(MSSV, ARXIV_ID)
        self.assertIsNone(self.store.begin_version(MSSV, ARXIV_ID, "v1"))
        repeated = self.store.record_source_result(self.result("v1", "timeout", error="ignored late result"))
        self.assertEqual(repeated, manifest)
        self.assertEqual((destination / "main.tex").read_bytes(), before)
        self.assertEqual(self.store.resume(MSSV, ARXIV_ID)["skip"], ["v1"])

    def test_source_success_does_not_complete_missing_metadata_references(self):
        self.discover(1)
        self.put_source()
        manifest = self.store.read_manifest(MSSV, ARXIV_ID)
        self.assertEqual(manifest["versions"]["v1"]["status"], "success")
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(manifest["references_status"], "not_attempted")

    def test_paper_complete_requires_every_stage(self):
        self.discover(1)
        self.put_source()
        self.store.save_metadata(MSSV, ARXIV_ID, self.complete_metadata())
        manifest = self.store.save_references(MSSV, ARXIV_ID, self.complete_references())
        self.assertEqual(manifest["status"], "complete")

    def test_confirmed_pdf_no_tex_needs_no_version_directory(self):
        self.discover(1)
        destination = self.store.begin_version(MSSV, ARXIV_ID, "v1")
        payload = Path(self.temp.name) / "pdf"
        payload.write_bytes(b"%PDF-1.4\nfixture only")
        extracted = arxiv_source.extract_source(payload, destination)
        self.store.record_source_result(self.result("v1", **extracted))
        self.assertFalse(destination.exists())
        self.assertTrue(self.store.should_skip_version(MSSV, ARXIV_ID, "v1"))
        self.store.save_metadata(MSSV, ARXIV_ID, self.complete_metadata())
        manifest = self.store.save_references(MSSV, ARXIV_ID, self.complete_references())
        self.assertEqual(manifest["status"], "complete_with_no_tex")

    def test_confirmed_withdrawn_version_needs_no_version_directory_and_paper_complete_with_withdrawn(self):
        self.discover(1)
        destination = self.store.begin_version(MSSV, ARXIV_ID, "v1")
        evidence = {
            "classification": "withdrawn_version",
            "is_terminal": True,
            "original_http_status": 404,
            "evidence_source": "arxiv_metadata",
            "reason": "Withdrawn by author",
            "details": {
                "arxiv_id": ARXIV_ID,
                "versioned_id": ARXIV_ID + "v1",
                "official_url": f"https://arxiv.org/abs/{ARXIV_ID}v1",
                "withdrawal_reason": "This paper has been withdrawn by the author.",
            },
        }
        res = self.result("v1", status="withdrawn_version", files=[], withdrawal_evidence=evidence)
        manifest = self.store.record_source_result(res)
        self.assertFalse(destination.exists())
        self.assertTrue(self.store.should_skip_version(MSSV, ARXIV_ID, "v1"))
        self.assertEqual(manifest["versions"]["v1"]["status"], "withdrawn_version")
        self.assertEqual(manifest["versions"]["v1"]["withdrawal_evidence"], evidence)
        self.store.save_metadata(MSSV, ARXIV_ID, self.complete_metadata())
        manifest = self.store.save_references(MSSV, ARXIV_ID, self.complete_references())
        self.assertEqual(manifest["status"], "complete_with_withdrawn")

    def test_withdrawn_status_rejects_keyword_without_version_scoped_evidence(self):
        self.discover(1)
        self.store.begin_version(MSSV, ARXIV_ID, "v1")
        evidence = {"classification": "withdrawn_version", "reason": "Withdrawn by author"}
        with self.assertRaises(StorageError):
            self.store.record_source_result(self.result(
                "v1", status="withdrawn_version", files=[], withdrawal_evidence=evidence,
            ))
        self.assertEqual(self.store.read_manifest(MSSV, ARXIV_ID)["versions"]["v1"]["status"], "in_progress")

    def test_bib_only_source_is_retained_and_no_tex_is_terminal(self):
        destination = self.put_source(entries={"refs.bib": BIB})
        self.assertEqual((destination / "refs.bib").read_bytes(), BIB)
        self.assertTrue(self.store.should_skip_version(MSSV, ARXIV_ID, "v1"))
        self.assertEqual(self.store.read_manifest(MSSV, ARXIV_ID)["versions"]["v1"]["status"], "no_tex_source")

    def test_fake_success_without_tex_is_not_committed(self):
        self.discover()
        self.store.begin_version(MSSV, ARXIV_ID, "v1")
        with self.assertRaises(IntegrityError):
            self.store.record_source_result(self.result("v1", "success"))
        self.assertEqual(self.store.read_manifest(MSSV, ARXIV_ID)["versions"]["v1"]["status"], "validation_failed")

    def test_empty_version_directory_is_not_confirmed_no_tex(self):
        self.discover()
        destination = self.store.begin_version(MSSV, ARXIV_ID, "v1")
        destination.mkdir()
        with self.assertRaises(IntegrityError):
            self.store.record_source_result(self.result("v1", "no_tex_source"))
        self.assertTrue(destination.exists())

    def test_missing_source_is_detected_without_resetting_checksums(self):
        destination = self.put_source()
        original = self.store.read_manifest(MSSV, ARXIV_ID)["versions"]["v1"]["file_checksums"]
        (destination / "main.tex").unlink()
        self.assertFalse(self.store.should_skip_version(MSSV, ARXIV_ID, "v1"))
        plan = self.store.resume(MSSV, ARXIV_ID)
        self.assertEqual(plan["blocked"]["v1"], "validation_failed")
        self.assertEqual(plan["manifest"]["versions"]["v1"]["file_checksums"], original)
        self.assertEqual((destination / "bib/refs.bib").read_bytes(), BIB)

    def test_same_size_corruption_is_detected_and_not_blessed(self):
        destination = self.put_source()
        corrupt = b"x" * len(TEX)
        (destination / "main.tex").write_bytes(corrupt)
        plan = self.store.resume(MSSV, ARXIV_ID)
        self.assertEqual(plan["blocked"]["v1"], "validation_failed")
        with self.assertRaises(StorageError):
            self.store.begin_version(MSSV, ARXIV_ID, "v1")
        self.assertEqual((destination / "main.tex").read_bytes(), corrupt)

    def test_extra_tex_file_invalidates_snapshot(self):
        destination = self.put_source()
        (destination / "unlisted.tex").write_bytes(TEX)
        self.assertFalse(self.store.should_skip_version(MSSV, ARXIV_ID, "v1"))

    def test_unexpected_file_type_is_rejected_without_deletion(self):
        self.discover()
        destination = self.store.begin_version(MSSV, ARXIV_ID, "v1")
        destination.mkdir()
        (destination / "main.tex").write_bytes(TEX)
        (destination / "figure.png").write_bytes(b"fixture")
        with self.assertRaises(IntegrityError):
            self.store.record_source_result(self.result("v1", "success", ["main.tex", "figure.png"]))
        self.assertTrue((destination / "figure.png").exists())

    def test_transient_failure_is_retryable_not_no_tex(self):
        self.discover()
        self.store.begin_version(MSSV, ARXIV_ID, "v1")
        self.store.record_source_result(
            self.result("v1", "server_error", error="503 fixture", http_status=503, attempts=3))
        plan = self.store.resume(MSSV, ARXIV_ID)
        self.assertIn("v1", plan["retry"])
        record = plan["manifest"]["versions"]["v1"]
        self.assertEqual(record["status"], "server_error")
        self.assertEqual(record["http_status"], 503)
        self.assertIsNone(record["archive_bytes"])

    def test_archive_failure_is_distinct_and_requires_audit(self):
        self.discover()
        self.store.begin_version(MSSV, ARXIV_ID, "v1")
        self.store.record_source_result(self.result("v1", "archive_error", error="corrupt fixture"))
        self.assertEqual(self.store.resume(MSSV, ARXIV_ID)["blocked"]["v1"], "archive_error")

    def test_resume_plan_for_success_error_and_pending_versions(self):
        self.put_source("v1")
        self.store.begin_version(MSSV, ARXIV_ID, "v2")
        self.store.record_source_result(
            self.result("v2", "server_error", error="503 fixture", http_status=503, attempts=3))
        plan = self.store.resume(MSSV, ARXIV_ID)
        self.assertEqual(plan["skip"], ["v1"])
        self.assertEqual(plan["retry"], ["v2", "v3"])
        self.assertEqual(plan["manifest"]["versions"]["v3"]["status"], "pending")
        self.assertEqual(plan["manifest"]["status"], "partial")

    def test_http_404_is_not_paper_not_found_or_no_tex(self):
        self.discover()
        self.store.begin_version(MSSV, ARXIV_ID, "v1")
        self.store.record_source_result(self.result("v1", "http_error", error="404", http_status=404))
        manifest = self.store.resume(MSSV, ARXIV_ID)["manifest"]
        self.assertEqual(manifest["discovery_status"], "found")
        self.assertEqual(manifest["versions"]["v1"]["status"], "http_error")

    def test_interrupted_download_without_output_can_restart(self):
        self.discover()
        self.store.begin_version(MSSV, ARXIV_ID, "v1")
        restarted = CorpusStorage(self.root, run_id="restart")
        plan = restarted.resume(MSSV, ARXIV_ID)
        self.assertEqual(plan["manifest"]["versions"]["v1"]["status"], "interrupted")
        self.assertEqual(set(plan["retry"]), {"v1", "v2", "v3"})
        self.assertFalse(restarted.begin_version(MSSV, ARXIV_ID, "v1").exists())

    def test_source_rename_before_manifest_commit_is_blocked_not_overwritten(self):
        self.discover()
        destination = self.store.begin_version(MSSV, ARXIV_ID, "v1")
        destination.mkdir()
        (destination / "main.tex").write_bytes(TEX)
        with patch.object(storage, "_atomic_json", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.record_source_result(self.result("v1", "success", ["main.tex"]))
        plan = CorpusStorage(self.root).resume(MSSV, ARXIV_ID)
        self.assertEqual(plan["blocked"]["v1"], "output_exists")
        self.assertEqual(plan["manifest"]["versions"]["v1"]["status"], "interrupted")
        self.assertEqual((destination / "main.tex").read_bytes(), TEX)

    def test_json_replace_interruption_leaves_old_manifest_valid(self):
        self.discover()
        path = self.store.paper_path(MSSV, ARXIV_ID) / "manifest.json"
        original = path.read_bytes()
        with patch.object(storage.os, "replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.begin_version(MSSV, ARXIV_ID, "v1")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.store.read_manifest(MSSV, ARXIV_ID)["versions"]["v1"]["status"], "pending")
        self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_partial_json_write_leaves_old_manifest_intact(self):
        self.discover()
        path = self.store.paper_path(MSSV, ARXIV_ID) / "manifest.json"
        original = path.read_bytes()
        def interrupted(payload, stream, **kwargs):
            stream.write("{")
            raise OSError("simulated full disk")
        with patch.object(storage.json, "dump", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.store.begin_version(MSSV, ARXIV_ID, "v1")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_resume_reconciles_metadata_written_before_manifest(self):
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        value.update(status="partial", title="Persisted fixture")
        original_writer = storage._atomic_json
        def interrupted(path, payload):
            if path.name == "manifest.json":
                raise KeyboardInterrupt
            return original_writer(path, payload)
        with patch.object(storage, "_atomic_json", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.store.save_metadata(MSSV, ARXIV_ID, value)
        self.assertEqual(self.store.read_manifest(MSSV, ARXIV_ID)["metadata_status"], "not_attempted")
        plan = CorpusStorage(self.root).resume(MSSV, ARXIV_ID)
        self.assertEqual(plan["manifest"]["metadata_status"], "partial")

    def test_resume_reconstructs_discovery_checkpoint_from_saved_metadata(self):
        original_writer = storage._atomic_json
        def interrupted(path, payload):
            if path.name == "manifest.json":
                raise KeyboardInterrupt
            return original_writer(path, payload)
        with patch.object(storage, "_atomic_json", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.discover()
        plan = CorpusStorage(self.root).resume(MSSV, ARXIV_ID)
        self.assertEqual(set(plan["retry"]), {"v1", "v2", "v3"})

    def test_unknown_version_and_unsafe_identifiers_are_rejected(self):
        for bad in ["../../escape", "/abs", "2502.08235v1", "2502/08235", "C:\\escape"]:
            with self.subTest(identifier=bad), self.assertRaises(StorageError):
                self.store.initialize_paper(MSSV, bad)
        self.discover()
        for bad in ["../v1", "/v1", "v0", "v01", "v1/../../x", "v4"]:
            with self.subTest(version=bad), self.assertRaises(StorageError):
                self.store.begin_version(MSSV, ARXIV_ID, bad)

    def test_unsafe_result_paths_are_refused_before_reading_source(self):
        self.discover()
        self.store.begin_version(MSSV, ARXIV_ID, "v1")
        for bad in ["../escape.tex", "/absolute.tex", "C:/file.tex", "a\\b.tex",
                    "dir/../file.tex", "CON.tex", "a//b.tex", "a./b.tex"]:
            with self.subTest(path=bad), self.assertRaises(StorageError):
                self.store.record_source_result(self.result("v1", "success", [bad]))

    def test_symlinked_paper_is_refused(self):
        paper = self.store.paper_path(MSSV, ARXIV_ID)
        original = Path.is_symlink
        def linked(path):
            return path == paper or original(path)
        with patch.object(Path, "is_symlink", linked):
            with self.assertRaises(IntegrityError):
                self.store.read_manifest(MSSV, ARXIV_ID)

    def test_source_hardlink_is_refused(self):
        self.discover()
        destination = self.store.begin_version(MSSV, ARXIV_ID, "v1")
        destination.mkdir()
        external = Path(self.temp.name) / "outside.tex"
        external.write_bytes(TEX)
        os.link(external, destination / "main.tex")
        with self.assertRaises(IntegrityError):
            self.store.record_source_result(self.result("v1", "success", ["main.tex"]))
        self.assertEqual(external.read_bytes(), TEX)

    def test_corrupt_or_foreign_manifest_fails_closed(self):
        path = self.store.paper_path(MSSV, ARXIV_ID) / "manifest.json"
        original = path.read_bytes()
        for bad in [b"{broken", b'{"contract_version":2,"contract_version":1}',
                    original.replace(MSSV.encode(), b"24120010"),
                    original.replace(b'"contract_version": 2', b'"contract_version": 1')]:
            path.write_bytes(bad)
            with self.subTest(data=bad[:30]), self.assertRaises(StorageError):
                self.store.resume(MSSV, ARXIV_ID)
            self.assertEqual(path.read_bytes(), bad)
        path.write_bytes(original)

    def test_orphaned_or_missing_json_is_not_reinitialized(self):
        path = self.store.paper_path(MSSV, ARXIV_ID) / "references.json"
        path.unlink()
        with self.assertRaises(StorageError):
            self.store.initialize_paper(MSSV, ARXIV_ID)
        self.assertFalse(path.exists())

    def test_schema_corruption_cannot_make_manifest_skippable(self):
        destination = self.put_source()
        path = self.store.paper_path(MSSV, ARXIV_ID) / "manifest.json"
        original = path.read_bytes()
        for change in ("missing_source_url", "invalid_attempts", "invalid_byte_count"):
            value = json.loads(original)
            record = value["versions"]["v1"]
            if change == "missing_source_url":
                del record["source_url"]
            elif change == "invalid_attempts":
                record["attempts"] = True
            else:
                record["kept_bytes"] = -1
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(change=change), self.assertRaises(StorageError):
                self.store.resume(MSSV, ARXIV_ID)
            self.assertEqual((destination / "main.tex").read_bytes(), TEX)
        path.write_bytes(original)


    def test_rediscovery_retains_sources_for_missing_fields(self):
        first = discovery()
        with patch.object(storage, "_now", return_value=DATE):
            self.store.record_discovery(first)
        later = discovery()
        later.update(title=None, authors=[], published=None,
                     missing_fields=["title", "authors", "published"])
        later_time = "2025-02-13T10:00:00Z"
        with patch.object(storage, "_now", return_value=later_time):
            self.store.record_discovery(later)
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        self.assertEqual(value["title"], first["title"])
        self.assertEqual(value["authors"], first["authors"])
        self.assertEqual(value["submission_date"], first["published"])
        provenance = value["provenance"]["arxiv"]
        self.assertEqual(provenance["snapshot"], later)
        self.assertEqual(provenance["retrieved_at"], later_time)
        for field in ("title", "authors", "published"):
            with self.subTest(field=field):
                source = provenance["field_sources"][field]
                self.assertEqual(source["query_identifier"], ARXIV_ID)
                self.assertEqual(source["retrieved_at"], DATE)
                self.assertEqual(source["snapshot"], first)

    def test_rediscovery_updates_values_and_sources_together(self):
        self.discover(1)
        later = discovery(1)
        later_time = "2025-02-14T11:00:00Z"
        later.update(title="Revised fixture title", authors=["Another Author"],
                     published="2025-02-11T08:00:00Z", updated=later_time)
        with patch.object(storage, "_now", return_value=later_time):
            self.store.record_discovery(later)
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        provenance = value["provenance"]["arxiv"]
        self.assertEqual(provenance["snapshot"], later)
        for field, target in (("title", "title"), ("authors", "authors"),
                              ("published", "submission_date")):
            with self.subTest(field=field):
                self.assertEqual(value[target], later[field])
                source = provenance["field_sources"][field]
                self.assertEqual(source["query_identifier"], ARXIV_ID)
                self.assertEqual(source["retrieved_at"], later_time)
                self.assertEqual(source["snapshot"], later)
        self.assertEqual(value["versions"][0]["date"], later["published"])
        self.assertEqual(value["versions"][0]["date_status"], "verified")

    def test_rediscovery_preserves_mixed_sources_across_runs(self):
        first = discovery()
        with patch.object(storage, "_now", return_value=DATE):
            self.store.record_discovery(first)
        second = discovery()
        second.update(title="New title only", authors=[], published=None,
                      missing_fields=["authors", "published"])
        second_time = "2025-02-13T10:00:00Z"
        with patch.object(storage, "_now", return_value=second_time):
            self.store.record_discovery(second)
        third = discovery()
        third.update(title=None, authors=[], published=None,
                     missing_fields=["title", "authors", "published"])
        self.store.record_discovery(third)
        value = CorpusStorage(self.root).read_metadata(MSSV, ARXIV_ID)
        provenance = value["provenance"]["arxiv"]
        self.assertEqual(provenance["snapshot"], third)
        self.assertEqual(value["title"], second["title"])
        self.assertEqual(value["authors"], first["authors"])
        self.assertEqual(value["submission_date"], first["published"])
        sources = provenance["field_sources"]
        self.assertEqual(sources["title"]["snapshot"], second)
        self.assertEqual(sources["title"]["retrieved_at"], second_time)
        for field in ("authors", "published"):
            self.assertEqual(sources[field]["snapshot"], first)
            self.assertEqual(sources[field]["retrieved_at"], DATE)
        self.assertTrue(all("field_sources" not in source for source in sources.values()))

    def test_rediscovery_preserves_legacy_v2_snapshot_evidence(self):
        first = discovery()
        self.store.record_discovery(first)
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        value["provenance"]["arxiv"].pop("field_sources", None)
        original_source = value["provenance"]["arxiv"]
        path = self.store.paper_path(MSSV, ARXIV_ID) / "metadata.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        later = discovery()
        later.update(title=None, authors=[], published=None,
                     missing_fields=["title", "authors", "published"])
        self.store.record_discovery(later)
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        self.assertEqual(value["provenance"]["arxiv"]["snapshot"], later)
        for field in ("title", "authors", "published"):
            self.assertEqual(value["provenance"]["arxiv"]["field_sources"][field],
                             original_source)

    def test_rediscovery_invalid_published_keeps_verified_source(self):
        first = discovery(1)
        self.store.record_discovery(first)
        original_source = self.store.read_metadata(MSSV, ARXIV_ID)["provenance"]["arxiv"]
        later = discovery(1)
        later["published"] = "not-an-ISO-date"
        self.store.record_discovery(later)
        value = self.store.read_metadata(MSSV, ARXIV_ID)
        self.assertEqual(value["submission_date"], first["published"])
        self.assertEqual(value["versions"][0]["date"], first["published"])
        self.assertEqual(value["versions"][0]["date_status"], "verified")
        provenance = value["provenance"]["arxiv"]
        self.assertEqual(provenance["snapshot"]["published"], "not-an-ISO-date")
        source = provenance["field_sources"]["published"]
        self.assertEqual(source["snapshot"], first)
        self.assertEqual(source["retrieved_at"], original_source["retrieved_at"])

    def test_manifest_object_status_fails_closed_without_changing_data(self):
        self.put_source()
        paper = self.store.paper_path(MSSV, ARXIV_ID)
        path = paper / "manifest.json"
        value = self.store.read_manifest(MSSV, ARXIV_ID)
        value["versions"]["v1"]["status"] = {}
        path.write_text(json.dumps(value), encoding="utf-8")
        before = {p.relative_to(paper).as_posix(): p.read_bytes()
                  for p in paper.rglob("*") if p.is_file()}
        actions = {
            "read": lambda: self.store.read_manifest(MSSV, ARXIV_ID),
            "resume": lambda: self.store.resume(MSSV, ARXIV_ID),
            "initialize": lambda: self.store.initialize_paper(MSSV, ARXIV_ID),
            "begin": lambda: self.store.begin_version(MSSV, ARXIV_ID, "v1"),
            "skip": lambda: self.store.should_skip_version(MSSV, ARXIV_ID, "v1"),
            "record": lambda: self.store.record_source_result(
                self.result("v1", "success", value["versions"]["v1"]["files"])),
        }
        for name, action in actions.items():
            with self.subTest(action=name):
                with self.assertRaisesRegex(StorageError, r"versions\.v1\.status"):
                    action()
                self.assertEqual(before, {p.relative_to(paper).as_posix(): p.read_bytes()
                                          for p in paper.rglob("*") if p.is_file()})

    def test_manifest_rejects_invalid_state_values_without_repair(self):
        self.discover(1)
        paper = self.store.paper_path(MSSV, ARXIV_ID)
        path = paper / "manifest.json"
        original = path.read_bytes()
        self.assertIsNone(self.store.read_manifest(MSSV, ARXIV_ID)["versions"]["v1"]["source_status"])
        fields = [
            ("status",), ("discovery_status",), ("version_status",),
            ("metadata_status",), ("references_status",),
            ("versions", "v1", "status"), ("versions", "v1", "source_status"),
        ]
        for keys in fields:
            for bad in ({}, [], ["success"], None, 0, 1.5, True, "", "unknown_state"):
                if keys[-1] == "source_status" and bad is None:
                    continue  # Null is a valid not-yet-recorded downloader result.
                with self.subTest(field=keys, value=bad):
                    value = json.loads(original)
                    target = value
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = bad
                    path.write_text(json.dumps(value), encoding="utf-8")
                    before = {p.name: p.read_bytes() for p in paper.glob("*.json")}
                    for action in (self.store.read_manifest, self.store.resume):
                        with self.assertRaisesRegex(StorageError, "expected a supported state string"):
                            action(MSSV, ARXIV_ID)
                        self.assertEqual(before, {p.name: p.read_bytes() for p in paper.glob("*.json")})

    def test_stage_checkpoints_validate_before_resume_writes(self):
        self.discover(1)
        metadata = self.store.read_metadata(MSSV, ARXIV_ID)
        metadata["status"] = "in_progress"
        self.store.save_metadata(MSSV, ARXIV_ID, metadata)
        paper = self.store.paper_path(MSSV, ARXIV_ID)
        originals = {p.name: p.read_bytes() for p in paper.glob("*.json")}
        fields = [
            ("metadata.json", ("status",)),
            ("metadata.json", ("version_status",)),
            ("metadata.json", ("versions", 0, "date_status")),
            ("metadata.json", ("provenance", "arxiv", "snapshot", "status")),
            ("references.json", ("status",)),
        ]
        for filename, keys in fields:
            for bad in ({}, [], ["partial"], None, 0, 1.5, True, "", "unknown_state"):
                with self.subTest(file=filename, field=keys, value=bad):
                    for name, content in originals.items():
                        (paper / name).write_bytes(content)
                    value = json.loads(originals[filename])
                    target = value
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = bad
                    (paper / filename).write_text(json.dumps(value), encoding="utf-8")
                    before = {p.name: p.read_bytes() for p in paper.glob("*.json")}
                    reader = (self.store.read_metadata if filename == "metadata.json"
                              else self.store.read_references)
                    for action in (reader, self.store.resume):
                        with self.assertRaisesRegex(StorageError, "expected a supported state string"):
                            action(MSSV, ARXIV_ID)
                        self.assertEqual(before, {p.name: p.read_bytes() for p in paper.glob("*.json")})

    def test_downloader_uses_storage_destination_with_mock_http_only(self):
        self.discover()
        destination = self.store.begin_version(MSSV, ARXIV_ID, "v1")
        payload = archive_bytes({"nested/main.tex": TEX})
        response = io.BytesIO(payload)
        response.status = 200
        response.headers = {"Content-Type": "application/gzip", "Content-Length": str(len(payload))}
        opener = Mock()
        opener.open.return_value = response
        with patch.object(arxiv_client, "_opener", opener), \
             patch.object(arxiv_client, "_next_request_at", 0.0), \
             patch.object(arxiv_client.time, "monotonic", return_value=1000.0):
            result = arxiv_source.download_source(MSSV, ARXIV_ID + "v1", destination)
        manifest = self.store.record_source_result(result)
        self.assertEqual(manifest["versions"]["v1"]["status"], "success")
        self.assertEqual(manifest["versions"]["v1"]["archive_bytes"], len(payload))
        self.assertEqual((destination / "nested/main.tex").read_bytes(), TEX)
        opener.open.assert_called_once()

    def test_resume_rejects_version_mismatch_before_any_checkpoint_write(self):
        self.discover(2)
        self.put_source("v1")
        paper = self.store.paper_path(MSSV, ARXIV_ID)
        originals = {p.name: p.read_bytes() for p in paper.glob("*.json")}
        for active in (("metadata",), ("references",), ("metadata", "references")):
            with self.subTest(in_progress=active):
                for name, content in originals.items():
                    (paper / name).write_bytes(content)
                metadata = json.loads(originals["metadata.json"])
                metadata.update(versions=metadata["versions"][:1], revised_dates=[])
                references = json.loads(originals["references.json"])
                for name, value in (("metadata", metadata), ("references", references)):
                    if name in active:
                        value["status"] = "in_progress"
                    (paper / (name + ".json")).write_text(
                        json.dumps(value), encoding="utf-8")
                # Each JSON is individually valid; only their coverage disagrees.
                self.assertEqual(len(self.store.read_metadata(MSSV, ARXIV_ID)["versions"]), 1)
                self.assertEqual(len(self.store.read_manifest(MSSV, ARXIV_ID)["versions"]), 2)
                self.store.read_references(MSSV, ARXIV_ID)
                before = {p.relative_to(paper).as_posix(): p.read_bytes()
                          for p in paper.rglob("*") if p.is_file()}
                with patch.object(storage, "_atomic_json", wraps=storage._atomic_json) as write:
                    for _ in range(2):
                        with self.assertRaisesRegex(StorageError, "cannot drop known versions"):
                            self.store.resume(MSSV, ARXIV_ID)
                        self.assertEqual(before, {
                            p.relative_to(paper).as_posix(): p.read_bytes()
                            for p in paper.rglob("*") if p.is_file()})
                    write.assert_not_called()

    def test_resume_rejects_missing_coverage_before_stage_recovery(self):
        self.discover(1)
        paper = self.store.paper_path(MSSV, ARXIV_ID)
        metadata = self.store.read_metadata(MSSV, ARXIV_ID)
        metadata.update(status="in_progress", version_status="needs_verification",
                        versions=[], revised_dates=None)
        references = self.store.read_references(MSSV, ARXIV_ID)
        references["status"] = "in_progress"
        for name, value in (("metadata", metadata), ("references", references)):
            (paper / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")
        self.store.read_metadata(MSSV, ARXIV_ID)
        self.store.read_references(MSSV, ARXIV_ID)
        self.store.read_manifest(MSSV, ARXIV_ID)
        before = {p.name: p.read_bytes() for p in paper.glob("*.json")}

        with patch.object(storage, "_atomic_json", wraps=storage._atomic_json) as write:
            with self.assertRaisesRegex(StorageError, "cannot drop known versions"):
                self.store.resume(MSSV, ARXIV_ID)
            write.assert_not_called()
        self.assertEqual(before, {p.name: p.read_bytes() for p in paper.glob("*.json")})

    def test_resume_recovers_newer_metadata_and_interrupted_stages(self):
        self.discover(3)
        paper = self.store.paper_path(MSSV, ARXIV_ID)
        manifest = self.store.read_manifest(MSSV, ARXIV_ID)
        manifest["versions"] = {"v1": manifest["versions"]["v1"]}
        (paper / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        metadata = self.store.read_metadata(MSSV, ARXIV_ID)
        provenance = metadata["provenance"]
        metadata["status"] = "in_progress"
        references = self.store.read_references(MSSV, ARXIV_ID)
        references["status"] = "in_progress"
        for name, value in (("metadata", metadata), ("references", references)):
            (paper / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")

        plan = self.store.resume(MSSV, ARXIV_ID)

        self.assertEqual(plan["retry"], ["v1", "v2", "v3"])
        self.assertEqual(plan["skip"], [])
        self.assertEqual(plan["blocked"], {})
        self.assertEqual(plan["manifest"]["metadata_status"], "interrupted")
        self.assertEqual(plan["manifest"]["references_status"], "interrupted")
        self.assertEqual(plan["manifest"]["status"], "interrupted")
        self.assertEqual(self.store.read_metadata(MSSV, ARXIV_ID)["provenance"], provenance)
        self.assertEqual(self.store.read_metadata(MSSV, ARXIV_ID)["status"], "interrupted")
        self.assertEqual(self.store.read_references(MSSV, ARXIV_ID)["status"], "interrupted")
        for name, record in plan["manifest"]["versions"].items():
            self.assertEqual(record["status"], "pending")
            self.assertFalse(self.store.version_destination(MSSV, ARXIV_ID, name).exists())
        before = {p.name: p.read_bytes() for p in paper.glob("*.json")}
        self.assertEqual(self.store.resume(MSSV, ARXIV_ID), plan)
        self.assertEqual(before, {p.name: p.read_bytes() for p in paper.glob("*.json")})


if __name__ == "__main__":
    unittest.main()
