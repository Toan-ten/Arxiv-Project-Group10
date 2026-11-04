"""Pipeline integration uses fake HTTP, real parser/extractor/Storage and TEMP."""
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from src import arxiv_client as client, arxiv_discovery, arxiv_source, storage
from src.pipeline import PipelineStopped, run_selected
from src.storage import CorpusStorage

PAIR = ("24120232", "2502.08235")
OTHER = ("24120035", "2507.16659")
THIRD = ("24120010", "2508.07872")
DATE = "2025-02-12T09:00:00Z"
TEX = b"\\documentclass{article}\r\n\\input{nested/method}\r\n% raw \xff\r\n"
BIB = b"@article{fixture, title={Offline only}}\r\n"


def atom(identifier=PAIR[1], versions=1):
    suffix = "" if versions is None else f"v{versions}"
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        f'<id>http://arxiv.org/abs/{identifier}{suffix}</id>'
        '<title>Offline fixture</title><author><name>Fixture Author</name></author>'
        f'<published>{DATE}</published><updated>{DATE}</updated></entry></feed>'
    ).encode()


def archive(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as handle:
        for name, body in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            handle.addfile(member, io.BytesIO(body))
    return data.getvalue()


class Response(io.BytesIO):
    def __init__(self, body, mime="application/octet-stream"):
        super().__init__(body)
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = mime
        self.headers["Content-Length"] = str(len(body))


def xml_response(identifier=PAIR[1], versions=1):
    return Response(atom(identifier, versions), "application/atom+xml")


def http_error(code, after=None):
    headers = Message()
    if after is not None:
        headers["Retry-After"] = str(after)
    return HTTPError(client.API_URL, code, "Offline fixture", headers, io.BytesIO())


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "corpus"
        self.now = 0.0
        self.outcomes, self.requests, self.starts, self.responses = [], [], [], []
        patches = [
            patch.object(client, "_next_request_at", 0.0),
            patch.object(client.time, "monotonic", lambda: self.now),
            patch.object(client.time, "time", lambda: 1700000000.0 + self.now),
            patch.object(client.time, "sleep", self.sleep),
            patch.object(client.random, "uniform", return_value=0.0),
        ]
        self.opener = Mock()
        self.opener.open.side_effect = self.open_response
        patches.append(patch.object(client, "_opener", self.opener))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def sleep(self, delay):
        self.assertGreaterEqual(delay, 0)
        self.now += delay

    def open_response(self, request, timeout):
        self.assertTrue(client._request_lock.locked())
        self.assertTrue(all(response.closed for response in self.responses))
        self.assertEqual(timeout, 15)
        self.starts.append(self.now)
        self.requests.append(request.full_url)
        self.assertTrue(self.outcomes, "Unexpected HTTP request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.responses.append(outcome)
        return outcome

    def run_pairs(self, pairs=None, **options):
        settings = dict(run_id="offline-fixture", timeout=15, max_attempts=1)
        settings.update(options)
        return run_selected([PAIR] if pairs is None else pairs, self.root, **settings)

    def store(self):
        return CorpusStorage(self.root, run_id="offline-fixture")

    def manifest(self, pair=PAIR):
        return self.store().read_manifest(*pair)

    def paper_path(self, pair=PAIR):
        return self.store().paper_path(*pair)

    def snapshot(self, pair=PAIR):
        paper = self.paper_path(pair)
        return {p.relative_to(paper).as_posix(): p.read_bytes()
                for p in paper.rglob("*") if p.is_file()}

    def seed(self, versions=2, completed=False):
        store = self.store()
        store.record_discovery(arxiv_discovery.parse_arxiv_response(atom(versions=versions), *PAIR))
        if completed:
            destination = store.begin_version(*PAIR, "v1")
            payload = Path(self.temp.name) / "seed.tar"
            payload.write_bytes(archive({"main.tex": TEX, "refs.bib": BIB}))
            result = arxiv_source.extract_source(payload, destination)
            result.update(mssv=PAIR[0], arxiv_id=PAIR[1], versioned_id=PAIR[1] + "v1")
            store.record_source_result(result)
        return store

    def test_fake_http_full_pipeline_saves_versions_bytes_and_checksums(self):
        self.outcomes = [
            xml_response(versions=3),
            Response(archive({"main.tex": TEX, "bib/refs.bib": BIB, "fig.png": b"PNG"})),
            Response(TEX), Response(archive({"nested/method.tex": TEX})),
        ]
        paper = self.run_pairs()["papers"][0]
        self.assertEqual(paper["discovery"]["action"], "queried")
        self.assertEqual(paper["discovery"]["version_status"], "resolved")
        self.assertEqual(paper["paper_status"], "partial")
        self.assertEqual(paper["references_status"], "not_attempted")
        self.assertEqual(paper["pending"], [])
        self.assertEqual(paper["blocked"], {})
        self.assertEqual(paper["errors"], [])
        self.assertEqual(self.starts, [0, 3, 6, 9])
        manifest = self.manifest()
        for name, record in manifest["versions"].items():
            self.assertEqual(record["status"], "success")
            self.assertEqual(record["run_id"], "offline-fixture")
            self.assertEqual(paper["source"][name]["action"], "downloaded")
            for relative in record["files"]:
                body = (self.paper_path() / name / relative).read_bytes()
                self.assertEqual(record["file_checksums"][relative], hashlib.sha256(body).hexdigest())
        self.assertEqual((self.paper_path() / "v1/main.tex").read_bytes(), TEX)
        self.assertEqual((self.paper_path() / "v1/bib/refs.bib").read_bytes(), BIB)
        self.assertFalse((self.paper_path() / "v1/fig.png").exists())
        references = self.store().read_references(*PAIR)
        self.assertEqual(references["items"], [])
        self.assertFalse(references["coverage"]["complete"])
        self.assertTrue(self.store().should_skip_version(*PAIR, "v3"))

    def test_two_papers_keep_ownership_and_output_separate(self):
        self.outcomes = [xml_response(), Response(TEX), xml_response(OTHER[1]), Response(TEX)]
        report = self.run_pairs([PAIR, OTHER])
        self.assertEqual([(p["mssv"], p["arxiv_id"]) for p in report["papers"]], [PAIR, OTHER])
        for pair in (PAIR, OTHER):
            self.assertEqual(self.manifest(pair)["mssv"], pair[0])
            self.assertEqual(self.store().read_metadata(*pair)["arxiv_id"], pair[1])
            self.assertTrue((self.paper_path(pair) / "v1/main.tex").exists())
        self.assertEqual(len(self.requests), 4)

    def test_second_run_skips_without_discovery_or_download(self):
        self.outcomes = [xml_response(), Response(TEX)]
        self.run_pairs()
        before = self.snapshot()
        paper = self.run_pairs()["papers"][0]
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(paper["discovery"]["action"], "cached")
        self.assertEqual(paper["skipped"], ["v1"])
        self.assertEqual(paper["source"]["v1"]["action"], "skipped")
        self.assertEqual(paper["source"]["v1"]["status"], "success")
        self.assertEqual(self.snapshot(), before)

    def test_resume_only_downloads_pending_versions(self):
        self.seed(versions=3, completed=True)
        before = (self.paper_path() / "v1/main.tex").read_bytes()
        self.outcomes = [Response(TEX), Response(TEX)]
        paper = self.run_pairs()["papers"][0]
        self.assertEqual(self.requests, [arxiv_source.SOURCE_URL + PAIR[1] + "v2",
                                        arxiv_source.SOURCE_URL + PAIR[1] + "v3"])
        self.assertEqual(paper["skipped"], ["v1"])
        self.assertEqual(paper["pending"], [])
        self.assertEqual((self.paper_path() / "v1/main.tex").read_bytes(), before)

    def test_network_failure_does_not_stop_next_version_and_retries_next_run(self):
        self.outcomes = [xml_response(versions=2), URLError(TimeoutError("fixture timeout")), Response(TEX)]
        first = self.run_pairs()["papers"][0]
        self.assertEqual(first["source"]["v1"]["status"], "timeout")
        self.assertEqual(first["source"]["v1"]["action"], "failed")
        self.assertEqual(first["source"]["v2"]["status"], "success")
        self.assertNotIn("v1", first["blocked"])
        self.outcomes = [Response(TEX)]
        second = self.run_pairs()["papers"][0]
        self.assertEqual(second["skipped"], ["v2"])
        self.assertEqual(self.requests[-1], arxiv_source.SOURCE_URL + PAIR[1] + "v1")
        self.assertEqual(len(self.requests), 4)

    def test_not_found_is_cached_and_does_not_download_or_complete_references(self):
        self.outcomes = [Response(
            b'<feed xmlns="http://www.w3.org/2005/Atom" '
            b'xmlns:o="http://a9.com/-/spec/opensearch/1.1/"><o:totalResults>0</o:totalResults></feed>',
            "application/atom+xml",
        )]
        first = self.run_pairs()["papers"][0]
        self.assertEqual(first["paper_status"], "not_found")
        self.assertEqual(first["source"], {})
        self.assertEqual(first["references_status"], "not_attempted")
        second = self.run_pairs()["papers"][0]
        self.assertEqual(second["discovery"]["action"], "cached")
        self.assertEqual(len(self.requests), 1)

    def test_unresolved_versions_remain_blocked_without_inventing_v1(self):
        self.outcomes = [xml_response(versions=None)]
        for _ in range(2):
            paper = self.run_pairs()["papers"][0]
            self.assertEqual(paper["blocked"]["discovery"], "needs_verification")
            self.assertEqual(paper["source"], {})
            self.assertEqual(self.manifest()["versions"], {})
        self.assertEqual(len(self.requests), 1)

    def test_no_tex_pdf_and_bib_only_are_terminal_but_paper_stays_partial(self):
        self.outcomes = [xml_response(versions=2), Response(b"%PDF-1.4\nfixture"),
                         Response(archive({"refs.bib": BIB}))]
        paper = self.run_pairs()["papers"][0]
        self.assertEqual([r["status"] for r in paper["source"].values()], ["no_tex_source"] * 2)
        self.assertEqual(paper["paper_status"], "partial")
        self.assertEqual(paper["errors"], [])
        self.assertFalse((self.paper_path() / "v1").exists())
        self.assertEqual((self.paper_path() / "v2/refs.bib").read_bytes(), BIB)
        self.assertEqual(self.run_pairs()["papers"][0]["skipped"], ["v1", "v2"])
        self.assertEqual(len(self.requests), 3)

    def test_discovery_network_failure_can_retry_on_next_run(self):
        self.outcomes = [URLError(TimeoutError("offline timeout"))]
        paper = self.run_pairs()["papers"][0]
        self.assertEqual(paper["discovery"]["status"], "timeout")
        self.assertEqual(paper["paper_status"], "failed")
        self.assertEqual(paper["source"], {})
        self.outcomes = [xml_response(), Response(TEX)]
        self.assertEqual(self.run_pairs()["papers"][0]["source"]["v1"]["status"], "success")

    def test_bad_discovery_response_does_not_lose_next_paper_progress(self):
        self.outcomes = [Response(b"<broken", "application/atom+xml"),
                         xml_response(OTHER[1]), Response(TEX)]
        report = self.run_pairs([PAIR, OTHER])
        self.assertEqual(report["papers"][0]["discovery"]["status"], "invalid_xml")
        self.assertEqual(report["papers"][0]["source"], {})
        self.assertEqual(report["papers"][1]["source"]["v1"]["status"], "success")

    def test_http_404_is_not_not_found_or_no_tex(self):
        self.outcomes = [xml_response(), http_error(404)]
        paper = self.run_pairs()["papers"][0]
        self.assertEqual(paper["discovery"]["status"], "found")
        self.assertEqual(paper["source"]["v1"]["status"], "http_error")
        self.assertEqual(paper["blocked"]["v1"], "http_error")
        self.assertEqual(self.manifest()["versions"]["v1"]["http_status"], 404)

    def test_archive_error_is_blocked_without_redownload(self):
        self.outcomes = [xml_response(versions=2), Response(b"PKbroken"), Response(TEX)]
        first = self.run_pairs()["papers"][0]
        self.assertEqual(first["source"]["v1"]["status"], "archive_error")
        self.assertEqual(first["source"]["v2"]["status"], "success")
        second = self.run_pairs()["papers"][0]
        self.assertEqual(second["blocked"]["v1"], "archive_error")
        self.assertEqual(second["skipped"], ["v2"])
        self.assertEqual(len(self.requests), 3)

    def test_corrupt_checksum_is_blocked_and_other_pending_version_continues(self):
        self.seed(completed=True)
        path = self.paper_path() / "v1/main.tex"
        original_hash = self.manifest()["versions"]["v1"]["file_checksums"]["main.tex"]
        path.write_bytes(b"x" * len(TEX))
        self.outcomes = [Response(TEX)]
        paper = self.run_pairs()["papers"][0]
        self.assertEqual(paper["blocked"]["v1"], "validation_failed")
        self.assertEqual(paper["source"]["v1"]["action"], "blocked")
        self.assertEqual(paper["source"]["v2"]["status"], "success")
        self.assertEqual(path.read_bytes(), b"x" * len(TEX))
        self.assertEqual(self.manifest()["versions"]["v1"]["file_checksums"]["main.tex"], original_hash)
        self.assertEqual(len(self.requests), 1)

    def test_missing_source_is_not_downloaded_over_or_marked_success(self):
        self.seed(versions=1, completed=True)
        (self.paper_path() / "v1/main.tex").unlink()
        paper = self.run_pairs()["papers"][0]
        self.assertEqual(paper["blocked"]["v1"], "validation_failed")
        self.assertEqual(paper["skipped"], [])
        self.assertEqual(self.requests, [])

    def test_orphan_destination_is_preserved_and_blocked(self):
        self.seed()
        orphan = self.paper_path() / "v1/main.tex"
        orphan.parent.mkdir()
        orphan.write_bytes(b"orphan bytes")
        self.outcomes = [Response(TEX)]
        paper = self.run_pairs()["papers"][0]
        self.assertEqual(paper["blocked"]["v1"], "output_exists")
        self.assertEqual(orphan.read_bytes(), b"orphan bytes")
        self.assertEqual(self.requests, [arxiv_source.SOURCE_URL + PAIR[1] + "v2"])

    def test_invalid_checkpoint_is_untouched_and_next_paper_runs(self):
        self.seed(versions=1)
        path = self.paper_path() / "manifest.json"
        path.write_bytes(b"{invalid checkpoint")
        before = self.snapshot()
        self.outcomes = [xml_response(OTHER[1]), Response(TEX)]
        report = self.run_pairs([PAIR, OTHER])
        self.assertIn("paper", report["papers"][0]["blocked"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(report["papers"][1]["source"]["v1"]["status"], "success")
        self.assertEqual(len(self.requests), 2)

    def test_wrong_checkpoint_state_is_not_reset_or_overwritten(self):
        self.seed(versions=1, completed=True)
        path = self.paper_path() / "manifest.json"
        value = self.manifest()
        value["versions"]["v1"]["status"] = {}
        path.write_text(json.dumps(value), encoding="utf-8")
        before = self.snapshot()
        paper = self.run_pairs()["papers"][0]
        self.assertIn("paper", paper["blocked"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.requests, [])

    def test_missing_mandatory_json_is_not_recreated(self):
        self.seed(versions=1)
        path = self.paper_path() / "references.json"
        path.unlink()
        before = self.snapshot()
        self.assertIn("paper", self.run_pairs()["papers"][0]["blocked"])
        self.assertFalse(path.exists())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.requests, [])

    def test_late_invalid_selection_is_rejected_before_http_and_checkpoint(self):
        invalid = [
            ("99999999", PAIR[1]), (OTHER[0], PAIR[1]), (PAIR[0], "2502.00001"),
            (PAIR[0], PAIR[1] + "v1"), (PAIR[0], "../escape"),
            (PAIR[0], "hep-th/9901001"), (PAIR[0], {}), ([], PAIR[1]),
        ]
        for bad in invalid:
            with self.subTest(selection=bad), self.assertRaises(ValueError):
                self.run_pairs([PAIR, bad])
            self.assertFalse(self.root.exists())
        self.assertEqual(self.requests, [])

    def test_duplicate_ids_are_rejected_before_http_and_checkpoint(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.run_pairs([PAIR, PAIR])
        self.assertEqual(self.requests, [])
        self.assertFalse(self.root.exists())

    def test_selection_shapes_are_validated_before_any_output(self):
        for bad in (None, "2502.08235", iter([PAIR]), [PAIR, (PAIR[0],)], [PAIR, "bad"]):
            with self.subTest(selection=repr(bad)), self.assertRaises(ValueError):
                run_selected(bad, self.root)
            self.assertFalse(self.root.exists())
        self.assertEqual(self.requests, [])

    def test_invalid_http_options_do_not_create_checkpoints(self):
        bad_options = [
            dict(timeout=False), dict(timeout=0), dict(timeout=float("nan")),
            dict(timeout=float("inf")), dict(max_attempts=True), dict(max_attempts=0),
            dict(user_agent="bad\nheader"), dict(user_agent=""), dict(user_agent=None),
        ]
        for options in bad_options:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_pairs(**options)
            self.assertFalse(self.root.exists())
        self.assertEqual(self.requests, [])

    def test_empty_selection_is_a_noop(self):
        report = self.run_pairs([])
        self.assertEqual(report["papers"], [])
        self.assertFalse(report["stopped"])
        self.assertFalse(self.root.exists())
        self.assertEqual(self.requests, [])

    def test_client_retry_is_reused_without_extra_pipeline_attempts(self):
        self.outcomes = [xml_response(), http_error(503), Response(TEX)]
        paper = self.run_pairs(max_attempts=2)["papers"][0]
        self.assertEqual(paper["source"]["v1"]["attempts"], 2)
        self.assertEqual(paper["source"]["v1"]["status"], "success")
        self.assertEqual(self.starts, [0, 3, 6])

    def test_retry_exhaustion_does_not_start_an_outer_retry_loop(self):
        self.outcomes = [xml_response(versions=2)] + [http_error(503) for _ in range(3)] + [Response(TEX)]
        paper = self.run_pairs(max_attempts=3)["papers"][0]
        self.assertEqual(paper["source"]["v1"]["status"], "server_error")
        self.assertEqual(paper["source"]["v1"]["attempts"], 3)
        self.assertEqual(paper["source"]["v2"]["status"], "success")
        self.assertEqual(self.starts, [0, 3, 6, 12, 15])

    def test_source_retry_after_is_honored_by_next_paper_discovery(self):
        self.outcomes = [xml_response(), http_error(429, 15),
                         xml_response(OTHER[1]), Response(TEX)]
        report = self.run_pairs([PAIR, OTHER])
        self.assertEqual(report["papers"][0]["source"]["v1"]["status"], "rate_limited")
        self.assertEqual(report["papers"][1]["source"]["v1"]["status"], "success")
        self.assertEqual(self.starts, [0, 3, 18, 21])

    def test_interrupt_during_download_leaves_intent_for_resume(self):
        self.outcomes = [xml_response(versions=2), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_pairs()
        self.assertEqual(self.manifest()["versions"]["v1"]["status"], "in_progress")
        self.assertEqual(self.manifest()["versions"]["v2"]["status"], "pending")
        self.outcomes = [Response(TEX), Response(TEX)]
        paper = self.run_pairs()["papers"][0]
        self.assertEqual(paper["discovery"]["action"], "cached")
        self.assertTrue(all(v["status"] == "success" for v in paper["source"].values()))

    def test_interrupt_after_source_before_checkpoint_never_overwrites_orphan(self):
        self.outcomes = [xml_response(), Response(TEX)]
        original = storage._atomic_json
        def interrupted(path, payload):
            record = (payload.get("versions", {}).get("v1", {})) if path.name == 'manifest.json' else {}
            if path.name == "manifest.json" and record.get("status") == "success":
                raise KeyboardInterrupt()
            return original(path, payload)
        with patch.object(storage, "_atomic_json", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_pairs()
        before = (self.paper_path() / "v1/main.tex").read_bytes()
        paper = self.run_pairs()["papers"][0]
        self.assertEqual(paper["blocked"]["v1"], "output_exists")
        self.assertEqual((self.paper_path() / "v1/main.tex").read_bytes(), before)
        self.assertEqual(len(self.requests), 2)

    def test_false_downloader_success_is_blocked_by_real_storage(self):
        self.outcomes = [xml_response()]
        fake = dict(mssv=PAIR[0], arxiv_id=PAIR[1], versioned_id=PAIR[1] + "v1",
                    status="success", files=[], error=None)
        with patch.object(arxiv_source, "download_source", return_value=fake):
            paper = self.run_pairs()["papers"][0]
        self.assertEqual(paper["blocked"]["v1"], "validation_failed")
        self.assertEqual(self.manifest()["versions"]["v1"]["status"], "validation_failed")

    def test_fatal_json_io_preserves_earlier_papers_and_stops_safely(self):
        self.outcomes = [xml_response(), Response(TEX), xml_response(OTHER[1])]
        original = storage._atomic_json
        def disk_full(path, payload):
            record = (payload.get("versions", {}).get("v1", {})) if path.name == 'manifest.json' else {}
            if payload.get("arxiv_id") == OTHER[1] and record.get("status") == "in_progress":
                raise OSError("fixture disk full")
            return original(path, payload)
        with patch.object(storage, "_atomic_json", side_effect=disk_full):
            with self.assertRaises(PipelineStopped) as caught:
                self.run_pairs([PAIR, OTHER, THIRD])
        summary = caught.exception.summary
        self.assertTrue(summary["stopped"])
        self.assertEqual(len(summary["papers"]), 2)
        self.assertEqual(summary["papers"][0]["source"]["v1"]["status"], "success")
        self.assertEqual(summary["papers"][1]["errors"][-1]["type"], "OSError")
        self.assertEqual(self.manifest()["versions"]["v1"]["status"], "success")
        self.assertEqual(self.manifest(OTHER)["versions"]["v1"]["status"], "pending")

    def test_downloader_storage_error_is_checkpointed_then_stops_run(self):
        self.outcomes = [xml_response(versions=2), Response(TEX)]
        with patch.object(arxiv_source, "_copy_limited",
                          side_effect=arxiv_source.SourceError("storage_error", "fixture disk full")):
            with self.assertRaises(PipelineStopped) as caught:
                self.run_pairs([PAIR, OTHER])
        paper = caught.exception.summary["papers"][0]
        self.assertTrue(caught.exception.summary["stopped"])
        self.assertEqual(paper["source"]["v1"]["status"], "storage_error")
        self.assertEqual(paper["pending"], ["v2"])
        self.assertEqual(self.manifest()["versions"]["v1"]["status"], "storage_error")
        self.assertEqual(len(self.requests), 2)

    def test_complete_existing_metadata_and_references_are_preserved(self):
        store = self.seed(versions=1, completed=True)
        metadata = store.read_metadata(*PAIR)
        metadata.update(status="complete", revised_dates=[], missing_fields=[])
        metadata["provenance"]["semantic_scholar"] = {"snapshot": {"fixture": True}}
        store.save_metadata(*PAIR, metadata)
        references = store.read_references(*PAIR)
        references.update(status="complete")
        references["coverage"].update(complete=True, continuation=None, reported_total=0)
        references["provenance"].update(query_identifier=PAIR[1], retrieved_at=DATE)
        store.save_references(*PAIR, references)
        before = self.snapshot()
        paper = self.run_pairs()["papers"][0]
        self.assertEqual(paper["paper_status"], "complete")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.requests, [])

    def test_resume_recovers_persisted_discovery_before_deciding_to_query(self):
        store = self.store()
        store.initialize_paper(*PAIR)
        original = storage._atomic_json
        def interrupted(path, payload):
            if path.name == "manifest.json":
                raise KeyboardInterrupt()
            return original(path, payload)
        with patch.object(storage, "_atomic_json", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                store.record_discovery(arxiv_discovery.parse_arxiv_response(atom(), *PAIR))
        self.assertEqual(self.manifest()["discovery_status"], "not_attempted")
        self.outcomes = [Response(TEX)]
        paper = self.run_pairs()["papers"][0]
        self.assertEqual(paper["discovery"]["action"], "cached")
        self.assertEqual(self.requests, [arxiv_source.SOURCE_URL + PAIR[1] + "v1"])

    def test_fatal_commit_after_source_preserves_output_and_reports_in_progress(self):
        self.outcomes = [xml_response(versions=2), Response(TEX)]
        original = storage._atomic_json
        def disk_full(path, payload):
            if path.name == "manifest.json":
                record = payload.get("versions", {}).get("v1", {})
                if record.get("status") == "success":
                    raise OSError("fixture commit failure after extraction")
            return original(path, payload)
        with patch.object(storage, "_atomic_json", side_effect=disk_full):
            with self.assertRaises(PipelineStopped) as caught:
                self.run_pairs()
        paper = caught.exception.summary["papers"][0]
        self.assertEqual(paper["source"]["v1"]["action"], "in_progress")
        self.assertEqual(paper["source"]["v1"]["status"], "in_progress")
        self.assertEqual(paper["pending"], ["v2"])
        self.assertEqual(self.manifest()["versions"]["v1"]["status"], "in_progress")
        before = (self.paper_path() / "v1/main.tex").read_bytes()
        self.outcomes = [Response(TEX)]
        resumed = self.run_pairs()["papers"][0]
        self.assertEqual(resumed["blocked"]["v1"], "output_exists")
        self.assertEqual(resumed["source"]["v2"]["status"], "success")
        self.assertEqual((self.paper_path() / "v1/main.tex").read_bytes(), before)
        self.assertEqual(len(self.requests), 3)

    def test_cached_null_provenance_is_blocked_and_next_paper_runs(self):
        store = self.seed(versions=1, completed=True)
        metadata = store.read_metadata(*PAIR)
        metadata["status"] = "in_progress"
        metadata["provenance"]["arxiv"] = None
        store.save_metadata(*PAIR, metadata)
        references = store.read_references(*PAIR)
        references["status"] = "in_progress"
        store.save_references(*PAIR, references)
        before = self.snapshot()
        self.outcomes = [xml_response(OTHER[1]), Response(TEX)]

        report = self.run_pairs([PAIR, OTHER], run_id="resume-new-run")

        self.assertFalse(report["stopped"])
        self.assertEqual(len(report["papers"]), 2)
        blocked, following = report["papers"]
        self.assertEqual(blocked["blocked"]["paper"], "StorageError")
        self.assertEqual(blocked["discovery"]["action"], "cached")
        self.assertEqual(blocked["discovery"]["status"], "found")
        self.assertEqual(blocked["discovery"]["version_status"], "resolved")
        self.assertEqual(blocked["errors"][0]["stage"], "checkpoint")
        self.assertEqual(blocked["errors"][0]["type"], "StorageError")
        self.assertIn("provenance", blocked["errors"][0]["message"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(following["source"]["v1"]["status"], "success")
        self.assertEqual(following["references_status"], "not_attempted")
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(OTHER[1] in url for url in self.requests))
        self.assertEqual(self.outcomes, [])

    def test_cached_null_provenance_blocks_unresolved_and_not_found(self):
        for status in ("unresolved", "not_found"):
            with self.subTest(discovery=status):
                self.root = Path(self.temp.name) / status
                store = self.store()
                result = (arxiv_discovery.parse_arxiv_response(
                    atom(versions=None), *PAIR) if status == "unresolved"
                    else dict(mssv=PAIR[0], arxiv_id=PAIR[1], status="not_found"))
                store.record_discovery(result)
                metadata = store.read_metadata(*PAIR)
                metadata["provenance"]["arxiv"] = None
                store.save_metadata(*PAIR, metadata)
                before = self.snapshot()

                report = self.run_pairs(run_id="resume-new-run")

                self.assertFalse(report["stopped"])
                self.assertEqual(report["papers"][0]["blocked"]["paper"], "StorageError")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.requests, [])

    def test_cached_missing_or_invalid_snapshot_is_preserved(self):
        store = self.seed(versions=1, completed=True)
        path = self.paper_path() / "metadata.json"
        original = path.read_bytes()
        for variant in ("missing", None, [], {}):
            with self.subTest(snapshot=variant):
                metadata = json.loads(original)
                if variant == "missing":
                    metadata["provenance"]["arxiv"].pop("snapshot")
                else:
                    metadata["provenance"]["arxiv"]["snapshot"] = variant
                path.write_text(json.dumps(metadata), encoding="utf-8")
                before = self.snapshot()

                report = self.run_pairs(run_id="resume-new-run")

                self.assertFalse(report["stopped"])
                self.assertIn("paper", report["papers"][0]["blocked"])
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.requests, [])

    def test_inconsistent_version_checkpoint_is_untouched_and_next_paper_runs(self):
        store = self.seed(versions=2, completed=True)
        metadata = store.read_metadata(*PAIR)
        metadata.update(status="in_progress", versions=metadata["versions"][:1],
                        revised_dates=[])
        references = store.read_references(*PAIR)
        references["status"] = "in_progress"
        for name, value in (("metadata", metadata), ("references", references)):
            (self.paper_path() / (name + ".json")).write_text(
                json.dumps(value), encoding="utf-8")
        self.assertEqual(len(store.read_metadata(*PAIR)["versions"]), 1)
        self.assertEqual(len(store.read_manifest(*PAIR)["versions"]), 2)
        self.assertEqual(store.read_references(*PAIR)["status"], "in_progress")
        before = self.snapshot()
        self.outcomes = [xml_response(OTHER[1]), Response(TEX)]

        report = self.run_pairs([PAIR, OTHER], run_id="resume-new-run")

        self.assertFalse(report["stopped"])
        self.assertEqual(len(report["papers"]), 2)
        self.assertEqual(report["papers"][0]["blocked"]["paper"], "StorageError")
        self.assertIn("cannot drop known versions", report["papers"][0]["errors"][0]["message"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(report["papers"][1]["source"]["v1"]["status"], "success")
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(OTHER[1] in url for url in self.requests))
        self.assertEqual(self.outcomes, [])


if __name__ == "__main__":
    unittest.main()
