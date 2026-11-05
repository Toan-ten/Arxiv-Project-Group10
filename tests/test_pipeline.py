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
        self.assertTrue(report["stopped"])
        self.assertEqual(report["stop_reason"]["http_status"], 429)
        self.assertEqual(len(report["papers"]), 1)
        self.assertEqual(report["papers"][0]["source"]["v1"]["status"], "rate_limited")
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertFalse(self.paper_path(OTHER).exists())
        self.assertEqual(self.starts, [0, 3])
        # A separate, explicitly initiated run still honors the shared cooldown.
        continued = self.run_pairs([OTHER])
        self.assertFalse(continued["stopped"])
        self.assertEqual(continued["papers"][0]["source"]["v1"]["status"], "success")
        self.assertEqual(self.opener.open.call_count, 4)
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

    def test_selected_v1_preserves_full_discovery_and_two_request_bound(self):
        pilot = ("24120232", "2502.09992")
        entries = {"main.tex": TEX, "nested/refs.bib": BIB}
        self.outcomes = [xml_response(pilot[1], versions=3), Response(archive(entries))]

        report = self.run_pairs([pilot], versions=("v1",), max_attempts=1)

        self.assertFalse(report["stopped"])
        paper = report["papers"][0]
        manifest = self.manifest(pilot)
        metadata = self.store().read_metadata(*pilot)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(self.requests, [
            client.API_URL + "?id_list=" + pilot[1] + "&start=0&max_results=1",
            arxiv_source.SOURCE_URL + pilot[1] + "v1",
        ])
        self.assertEqual(self.starts, [0, 3])
        self.assertEqual(self.outcomes, [])
        self.assertEqual((metadata["mssv"], metadata["arxiv_id"]), pilot)
        self.assertEqual((manifest["mssv"], manifest["arxiv_id"]), pilot)
        expected_ids = [pilot[1] + name for name in ("v1", "v2", "v3")]
        self.assertEqual([v["versioned_id"] for v in metadata["versions"]], expected_ids)
        self.assertEqual(metadata["provenance"]["arxiv"]["snapshot"]["version_ids"], expected_ids)
        self.assertEqual(set(manifest["versions"]), {"v1", "v2", "v3"})
        self.assertEqual(paper["pending"], ["v2", "v3"])
        self.assertEqual(paper["paper_status"], "partial")
        self.assertEqual(paper["references_status"], "not_attempted")
        self.assertEqual(self.store().read_references(*pilot)["items"], [])
        record = manifest["versions"]["v1"]
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["kept_bytes"], sum(map(len, entries.values())))
        for name, content in entries.items():
            self.assertEqual((self.paper_path(pilot) / "v1" / name).read_bytes(), content)
            self.assertEqual(record["file_checksums"][name], hashlib.sha256(content).hexdigest())
        for name in ("v2", "v3"):
            record = manifest["versions"][name]
            self.assertEqual(record["status"], "pending")
            self.assertEqual(record["attempts"], 0)
            self.assertIsNone(record["source_status"])
            self.assertFalse((self.paper_path(pilot) / name).exists())

    def test_selected_v1_resume_sends_no_new_requests_with_versions_pending(self):
        pilot = ("24120232", "2502.09992")
        self.outcomes = [xml_response(pilot[1], versions=3), Response(TEX)]
        self.run_pairs([pilot], versions=("v1",), max_attempts=1)
        before = self.snapshot(pilot)
        calls = self.opener.open.call_count
        requests = list(self.requests)

        paper = self.run_pairs([pilot], versions=("v1",), max_attempts=1)["papers"][0]

        self.assertEqual(calls, 2)
        self.assertEqual(self.opener.open.call_count, calls)
        self.assertEqual(self.requests, requests)
        self.assertEqual(self.snapshot(pilot), before)
        self.assertEqual(paper["discovery"]["action"], "cached")
        self.assertEqual(paper["skipped"], ["v1"])
        self.assertEqual(paper["source"]["v1"]["action"], "skipped")
        self.assertEqual(paper["pending"], ["v2", "v3"])
        self.assertEqual(paper["paper_status"], "partial")
        for name in ("v2", "v3"):
            self.assertEqual(self.manifest(pilot)["versions"][name]["status"], "pending")
            self.assertFalse((self.paper_path(pilot) / name).exists())

    def test_selection_uses_exact_names_and_supports_multiple_versions(self):
        self.outcomes = [xml_response(versions=3), Response(TEX)]
        first = self.run_pairs(versions=["v3"])["papers"][0]
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(self.requests[-1], arxiv_source.SOURCE_URL + PAIR[1] + "v3")
        self.assertEqual(first["pending"], ["v1", "v2"])
        self.assertFalse((self.paper_path() / "v1").exists())
        self.assertFalse((self.paper_path() / "v2").exists())
        completed = (self.paper_path() / "v3/main.tex").read_bytes()
        self.outcomes = [Response(TEX)]

        second = self.run_pairs(versions=("v3", "v1"))["papers"][0]

        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual(self.requests[-1], arxiv_source.SOURCE_URL + PAIR[1] + "v1")
        self.assertEqual(second["pending"], ["v2"])
        self.assertEqual(second["skipped"], ["v3"])
        self.assertEqual((self.paper_path() / "v3/main.tex").read_bytes(), completed)
        self.assertFalse((self.paper_path() / "v2").exists())

    def test_invalid_version_selections_fail_before_http_or_checkpoint_writes(self):
        invalid = [
            "", "v1", 1, True, {}, {"v1"}, (), [], iter(("v1",)),
            (None,), ({},), (1,), ("v1", "v1"), ("v0",), ("v01",),
            ("V1",), ("v-1",), ("v1 ",), ("v1\n",), ("v1/v2",),
            ("../v1",), ("2502.09992v1",), ("v1", []),
        ]
        for versions in invalid:
            with self.subTest(versions=versions):
                with self.assertRaises(ValueError):
                    self.run_pairs(versions=versions)
                self.opener.open.assert_not_called()
                self.assertFalse(self.root.exists())
        self.seed(versions=3, completed=True)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.run_pairs(versions=("v1", "v0"))
        self.assertEqual(self.snapshot(), before)
        self.opener.open.assert_not_called()

    def test_selected_undiscovered_version_blocks_source_and_keeps_checkpoint(self):
        self.outcomes = [xml_response(versions=3)]
        with patch.object(arxiv_source, "download_source", wraps=arxiv_source.download_source) as download:
            first = self.run_pairs(versions=("v1", "v4"))["papers"][0]
            download.assert_not_called()
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(first["blocked"]["v4"], "not_discovered")
        self.assertEqual(first["errors"][0]["version"], "v4")
        self.assertEqual(first["pending"], ["v1", "v2", "v3"])
        self.assertEqual(set(self.manifest()["versions"]), {"v1", "v2", "v3"})
        for name in ("v1", "v2", "v3", "v4"):
            self.assertFalse((self.paper_path() / name).exists())
        before = self.snapshot()

        second = self.run_pairs(versions=("v4",))["papers"][0]

        self.assertEqual(second["blocked"]["v4"], "not_discovered")
        self.assertEqual(second["discovery"]["action"], "cached")
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self.snapshot(), before)

    def test_selected_v1_not_found_and_unresolved_never_call_downloader(self):
        for status in ("not_found", "unresolved"):
            with self.subTest(discovery=status):
                self.root = Path(self.temp.name) / status
                self.opener.open.reset_mock()
                response = (Response(
                    b'<feed xmlns="http://www.w3.org/2005/Atom" '
                    b'xmlns:o="http://a9.com/-/spec/opensearch/1.1/">'
                    b'<o:totalResults>0</o:totalResults></feed>', "application/atom+xml")
                    if status == "not_found" else xml_response(versions=None))
                self.outcomes = [response]
                with patch.object(arxiv_source, "download_source",
                                  wraps=arxiv_source.download_source) as download:
                    paper = self.run_pairs(versions=("v1",))["papers"][0]
                    download.assert_not_called()
                self.assertEqual(self.opener.open.call_count, 1)
                self.assertEqual(self.manifest()["versions"], {})
                self.assertFalse((self.paper_path() / "v1").exists())
                self.assertEqual(paper["references_status"], "not_attempted")
                if status == "not_found":
                    self.assertEqual(paper["paper_status"], "not_found")
                else:
                    self.assertEqual(paper["blocked"]["discovery"], "needs_verification")

    def test_selected_v1_discovery_errors_use_one_transport_attempt(self):
        for code, expected in ((None, "timeout"), (403, "http_error"),
                               (429, "rate_limited"), (503, "server_error"),
                               (302, "http_error")):
            with self.subTest(http_status=code):
                self.root = Path(self.temp.name) / str(code)
                self.opener.open.reset_mock()
                failure = URLError(TimeoutError("fixture timeout")) if code is None else http_error(code)
                if code == 302:
                    failure.headers["Location"] = "https://arxiv.org/abs/" + PAIR[1]
                self.outcomes = [failure]
                with patch.object(arxiv_source, "download_source",
                                  wraps=arxiv_source.download_source) as download:
                    report = self.run_pairs(versions=("v1",), max_attempts=1)
                    paper = report["papers"][0]
                    download.assert_not_called()
                self.assertEqual(report["stopped"], code in (403, 429))
                if code in (403, 429):
                    self.assertEqual(report["stop_reason"]["http_status"], code)
                    self.assertEqual(report["stop_reason"]["stage"], "discovery")
                self.assertEqual(self.opener.open.call_count, 1)
                self.assertEqual(paper["discovery"]["status"], expected)
                self.assertEqual(paper["discovery"]["attempts"], 1)
                self.assertEqual(self.manifest()["versions"], {})
                self.assertEqual(self.outcomes, [])
                self.assertFalse((self.paper_path() / "v1").exists())

    def test_selected_v1_source_errors_do_not_retry_or_download_other_versions(self):
        for code, expected in ((None, "timeout"), (403, "http_error"),
                               (429, "rate_limited"), (503, "server_error"),
                               (302, "http_error")):
            with self.subTest(http_status=code):
                self.root = Path(self.temp.name) / str(code)
                self.opener.open.reset_mock()
                failure = URLError(TimeoutError("fixture timeout")) if code is None else http_error(code)
                if code == 302:
                    failure.headers["Location"] = "https://arxiv.org/src/" + PAIR[1] + "v1"
                self.outcomes = [xml_response(versions=3), failure]

                report = self.run_pairs(versions=("v1",), max_attempts=1)
                paper = report["papers"][0]

                self.assertEqual(report["stopped"], code in (403, 429))
                if code in (403, 429):
                    self.assertEqual(report["stop_reason"]["http_status"], code)
                    self.assertEqual(report["stop_reason"]["stage"], "source")
                self.assertEqual(self.opener.open.call_count, 2)
                self.assertEqual(self.requests[-1], arxiv_source.SOURCE_URL + PAIR[1] + "v1")
                self.assertEqual(paper["source"]["v1"]["status"], expected)
                self.assertEqual(paper["source"]["v1"]["attempts"], 1)
                self.assertEqual(paper["pending"], ["v2", "v3"])
                self.assertEqual(paper["references_status"], "not_attempted")
                self.assertNotIn(paper["paper_status"], {"complete", "complete_with_no_tex"})
                self.assertEqual(self.outcomes, [])
                for name in ("v1", "v2", "v3"):
                    self.assertFalse((self.paper_path() / name).exists())

    def test_selected_v1_corrupt_checkpoints_are_preserved_without_http(self):
        for corruption in ("invalid_json", "null_provenance"):
            with self.subTest(checkpoint=corruption):
                self.root = Path(self.temp.name) / corruption
                store = self.seed(versions=3, completed=True)
                if corruption == "invalid_json":
                    (self.paper_path() / "manifest.json").write_bytes(b"{invalid")
                else:
                    metadata = store.read_metadata(*PAIR)
                    metadata["provenance"]["arxiv"] = None
                    store.save_metadata(*PAIR, metadata)
                before = self.snapshot()

                paper = self.run_pairs(versions=("v1",))["papers"][0]

                self.assertIn("paper", paper["blocked"])
                self.assertEqual(self.snapshot(), before)
                self.opener.open.assert_not_called()

    def test_selected_v1_checksum_damage_blocks_without_other_downloads(self):
        self.seed(versions=3, completed=True)
        source = self.paper_path() / "v1/main.tex"
        source.write_bytes(TEX + b"fixture corruption")
        before = source.read_bytes()

        paper = self.run_pairs(versions=("v1",))["papers"][0]

        self.assertEqual(paper["blocked"]["v1"], "validation_failed")
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(paper["pending"], ["v2", "v3"])
        self.assertEqual(self.manifest()["versions"]["v1"]["status"], "validation_failed")
        self.opener.open.assert_not_called()
        for name in ("v2", "v3"):
            self.assertFalse((self.paper_path() / name).exists())

    def test_selected_v1_orphan_is_not_overwritten_or_downloaded_around(self):
        self.seed(versions=3)
        orphan = self.paper_path() / "v1/main.tex"
        orphan.parent.mkdir()
        orphan.write_bytes(b"orphan fixture")
        before = self.snapshot()

        paper = self.run_pairs(versions=("v1",))["papers"][0]

        self.assertEqual(paper["blocked"]["v1"], "output_exists")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(paper["pending"], ["v1", "v2", "v3"])
        self.opener.open.assert_not_called()
        for name in ("v2", "v3"):
            self.assertFalse((self.paper_path() / name).exists())

    def test_versions_none_or_omitted_keeps_processing_all_versions(self):
        for mode, options in (("omitted", {}), ("explicit_none", {"versions": None})):
            with self.subTest(selection=mode):
                self.root = Path(self.temp.name) / mode
                self.opener.open.reset_mock()
                self.outcomes = [xml_response(versions=3)] + [Response(TEX) for _ in range(3)]

                paper = self.run_pairs(**options)["papers"][0]

                self.assertEqual(self.opener.open.call_count, 4)
                self.assertEqual(paper["pending"], [])
                self.assertEqual(set(paper["source"]), {"v1", "v2", "v3"})
                for name, record in self.manifest()["versions"].items():
                    self.assertEqual(record["status"], "success")
                    self.assertTrue((self.paper_path() / name / "main.tex").exists())

    def test_selected_no_tex_is_terminal_and_resume_does_not_advance(self):
        self.outcomes = [xml_response(versions=3), Response(b"%PDF-1.7\nfixture")]

        first = self.run_pairs(versions=("v1",))["papers"][0]

        self.assertEqual(first["source"]["v1"]["status"], "no_tex_source")
        self.assertEqual(first["pending"], ["v2", "v3"])
        self.assertFalse((self.paper_path() / "v1").exists())
        before = self.snapshot()
        second = self.run_pairs(versions=("v1",))["papers"][0]
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(second["skipped"], ["v1"])
        self.assertEqual(second["pending"], ["v2", "v3"])
        self.assertEqual(second["references_status"], "not_attempted")
        self.assertEqual(second["paper_status"], "partial")
        self.assertEqual(self.snapshot(), before)

    def test_selection_preserves_existing_unselected_success_and_error_states(self):
        store = self.seed(versions=3, completed=True)
        source = self.paper_path() / "v1/main.tex"
        before = source.read_bytes()
        store.begin_version(*PAIR, "v2")
        store.record_source_result(dict(
            mssv=PAIR[0], arxiv_id=PAIR[1], versioned_id=PAIR[1] + "v2",
            status="timeout", files=[], error="fixture timeout", attempts=1,
        ))
        previous_v2 = self.manifest()["versions"]["v2"]
        self.outcomes = [Response(TEX)]

        paper = self.run_pairs(versions=("v3",))["papers"][0]

        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self.requests, [arxiv_source.SOURCE_URL + PAIR[1] + "v3"])
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.manifest()["versions"]["v1"]["status"], "success")
        self.assertEqual(self.manifest()["versions"]["v2"], previous_v2)
        self.assertEqual(paper["source"]["v3"]["status"], "success")
        self.assertFalse((self.paper_path() / "v2").exists())

    def test_second_discovery_403_stops_before_third_paper_after_checkpoint(self):
        self.outcomes = [xml_response(), Response(TEX), http_error(403),
                         xml_response(THIRD[1]), Response(TEX)]

        report = self.run_pairs([PAIR, OTHER, THIRD], max_attempts=1)

        self.assertTrue(report["stopped"])
        self.assertEqual(report["stop_reason"], dict(
            type="http_policy", stage="discovery", http_status=403,
            mssv=OTHER[0], arxiv_id=OTHER[1], version=None, checkpoint_saved=True,
        ))
        self.assertEqual(len(report["papers"]), 2)
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual(len(self.outcomes), 2)
        self.assertEqual(self.starts, [0, 3, 6])
        self.assertFalse(self.paper_path(THIRD).exists())
        middle = report["papers"][1]
        self.assertEqual(middle["discovery"]["status"], "http_error")
        self.assertEqual(middle["discovery"]["http_status"], 403)
        self.assertEqual(middle["errors"][0]["type"], "http_error")
        self.assertEqual(middle["source"], {})
        self.assertEqual(middle["paper_status"], "failed")
        self.assertEqual(self.manifest(OTHER)["discovery_status"], "http_error")
        metadata = self.store().read_metadata(*OTHER)
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(metadata["provenance"]["arxiv"]["snapshot"]["http_status"], 403)
        self.assertEqual(self.store().read_references(*OTHER)["status"], "not_attempted")
        self.assertEqual((self.paper_path() / "v1/main.tex").read_bytes(), TEX)
        self.assertTrue(self.store().should_skip_version(*PAIR, "v1"))
        self.assertEqual(self.manifest()["versions"]["v1"]["file_checksums"]["main.tex"],
                         hashlib.sha256(TEX).hexdigest())
        self.assertEqual(report["papers"][0]["paper_status"], "partial")

    def test_second_source_429_stops_before_next_version_and_third_paper(self):
        self.outcomes = [
            xml_response(), Response(TEX), xml_response(OTHER[1], versions=2),
            http_error(429, 15), Response(TEX), xml_response(THIRD[1]), Response(TEX),
        ]

        report = self.run_pairs([PAIR, OTHER, THIRD], max_attempts=1)

        self.assertTrue(report["stopped"])
        self.assertEqual(report["stop_reason"], dict(
            type="http_policy", stage="source", http_status=429,
            mssv=OTHER[0], arxiv_id=OTHER[1], version="v1", checkpoint_saved=True,
        ))
        self.assertEqual(len(report["papers"]), 2)
        self.assertEqual(self.opener.open.call_count, 4)
        self.assertEqual(len(self.outcomes), 3)
        self.assertEqual(self.starts, [0, 3, 6, 9])
        self.assertFalse(self.paper_path(THIRD).exists())
        middle = report["papers"][1]
        self.assertEqual(middle["source"]["v1"]["action"], "failed")
        self.assertEqual(middle["source"]["v1"]["status"], "rate_limited")
        self.assertEqual(middle["source"]["v1"]["http_status"], 429)
        self.assertEqual(middle["source"]["v1"]["attempts"], 1)
        self.assertEqual(middle["pending"], ["v2"])
        manifest = self.manifest(OTHER)
        self.assertEqual(manifest["versions"]["v1"]["status"], "rate_limited")
        self.assertEqual(manifest["versions"]["v1"]["http_status"], 429)
        self.assertIsNone(manifest["versions"]["v1"]["completed_at"])
        self.assertEqual(manifest["versions"]["v2"]["status"], "pending")
        self.assertEqual(manifest["versions"]["v2"]["attempts"], 0)
        for name in ("v1", "v2"):
            self.assertFalse((self.paper_path(OTHER) / name).exists())
        self.assertEqual(middle["references_status"], "not_attempted")
        self.assertEqual(middle["paper_status"], "partial")
        self.assertEqual((self.paper_path() / "v1/main.tex").read_bytes(), TEX)
        self.assertTrue(self.store().should_skip_version(*PAIR, "v1"))

    def test_http_policy_stops_at_both_stages_with_default_or_selected_versions(self):
        for stage in ("discovery", "source"):
            for code in (403, 429):
                for versions in (None, ("v1",)):
                    with self.subTest(stage=stage, http_status=code, versions=versions):
                        self.root = Path(self.temp.name) / (stage + str(code) + str(versions))
                        self.opener.open.reset_mock()
                        self.outcomes = ([http_error(code)] if stage == "discovery" else
                                         [xml_response(versions=3), http_error(code)])
                        self.outcomes.extend([xml_response(OTHER[1]), Response(TEX)])

                        report = self.run_pairs([PAIR, OTHER], versions=versions, max_attempts=1)

                        self.assertTrue(report["stopped"])
                        self.assertEqual(report["stop_reason"]["stage"], stage)
                        self.assertEqual(report["stop_reason"]["http_status"], code)
                        self.assertEqual(report["stop_reason"]["arxiv_id"], PAIR[1])
                        self.assertEqual(len(report["papers"]), 1)
                        self.assertEqual(self.opener.open.call_count, 1 if stage == "discovery" else 2)
                        self.assertEqual(len(self.outcomes), 2)
                        self.assertFalse(self.paper_path(OTHER).exists())
                        paper = report["papers"][0]
                        self.assertEqual(paper["references_status"], "not_attempted")
                        self.assertNotIn(paper["paper_status"], {"complete", "complete_with_no_tex"})
                        if stage == "source":
                            self.assertEqual(paper["pending"], ["v2", "v3"])
                            self.assertEqual(self.manifest()["versions"]["v1"]["http_status"], code)
                            for name in ("v2", "v3"):
                                self.assertFalse((self.paper_path() / name).exists())

    def test_not_found_and_404_continue_to_third_paper(self):
        for failure in ("not_found", "discovery_404", "source_404"):
            with self.subTest(second_paper=failure):
                self.root = Path(self.temp.name) / failure
                self.opener.open.reset_mock()
                if failure == "not_found":
                    middle = [Response(
                        b'<feed xmlns="http://www.w3.org/2005/Atom" '
                        b'xmlns:o="http://a9.com/-/spec/opensearch/1.1/">'
                        b'<o:totalResults>0</o:totalResults></feed>', "application/atom+xml")]
                elif failure == "discovery_404":
                    middle = [http_error(404)]
                else:
                    middle = [xml_response(OTHER[1], versions=2), http_error(404), Response(TEX)]
                expected_requests = 4 + len(middle)
                self.outcomes = ([xml_response(), Response(TEX)] + middle +
                                 [xml_response(THIRD[1]), Response(TEX)])

                report = self.run_pairs([PAIR, OTHER, THIRD], max_attempts=1)

                self.assertFalse(report["stopped"])
                self.assertNotIn("stop_reason", report)
                self.assertEqual(len(report["papers"]), 3)
                self.assertEqual(self.opener.open.call_count, expected_requests)
                self.assertEqual(report["papers"][2]["source"]["v1"]["status"], "success")
                if failure == "not_found":
                    self.assertEqual(report["papers"][1]["paper_status"], "not_found")
                elif failure == "source_404":
                    self.assertEqual(report["papers"][1]["source"]["v1"]["http_status"], 404)
                    self.assertEqual(report["papers"][1]["source"]["v2"]["status"], "success")
                else:
                    self.assertEqual(report["papers"][1]["discovery"]["http_status"], 404)
                self.assertEqual(self.outcomes, [])

    def test_resume_after_policy_stop_skips_good_source_and_keeps_unselected_pending(self):
        self.outcomes = [
            xml_response(), Response(TEX), xml_response(OTHER[1], versions=2), http_error(429, 15),
        ]
        first = self.run_pairs([PAIR, OTHER, THIRD], versions=("v1",), max_attempts=1)
        self.assertTrue(first["stopped"])
        before = self.snapshot()
        self.assertEqual(self.opener.open.call_count, 4)
        self.outcomes = [Response(TEX), xml_response(THIRD[1]), Response(TEX)]

        # Explicit offline resume models a separately authorized later invocation.
        second = self.run_pairs([PAIR, OTHER, THIRD], versions=("v1",), max_attempts=1)

        self.assertFalse(second["stopped"])
        self.assertNotIn("stop_reason", second)
        self.assertEqual(self.opener.open.call_count, 7)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(second["papers"][0]["skipped"], ["v1"])
        self.assertEqual(second["papers"][0]["discovery"]["action"], "cached")
        self.assertEqual(second["papers"][1]["discovery"]["action"], "cached")
        self.assertEqual(second["papers"][1]["source"]["v1"]["status"], "success")
        self.assertEqual(second["papers"][1]["pending"], ["v2"])
        self.assertEqual(second["papers"][2]["source"]["v1"]["status"], "success")
        self.assertEqual(self.starts[4], 24)
        self.assertFalse((self.paper_path(OTHER) / "v2").exists())
        for paper in second["papers"]:
            self.assertEqual(paper["references_status"], "not_attempted")
            self.assertEqual(paper["paper_status"], "partial")

    def test_checkpoint_write_failure_on_403_remains_a_fatal_pipeline_stopped(self):
        self.seed(versions=1, completed=True)
        before = self.snapshot()
        self.outcomes = [http_error(403), xml_response(THIRD[1]), Response(TEX)]
        original = storage._atomic_json
        def disk_full(path, payload):
            if (path.name == "manifest.json" and payload.get("arxiv_id") == OTHER[1]
                    and payload.get("discovery_status") == "http_error"):
                raise OSError("fixture cannot commit HTTP error")
            return original(path, payload)
        with patch.object(storage, "_atomic_json", side_effect=disk_full):
            with self.assertRaises(PipelineStopped) as caught:
                self.run_pairs([PAIR, OTHER, THIRD], max_attempts=1)

        report = caught.exception.summary
        self.assertTrue(report["stopped"])
        self.assertNotIn("stop_reason", report)
        self.assertEqual(report["papers"][1]["errors"][-1]["type"], "OSError")
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.paper_path(THIRD).exists())
        self.assertEqual(self.manifest(OTHER)["discovery_status"], "not_attempted")
        self.assertEqual(self.store().read_metadata(*OTHER)["provenance"]["arxiv"]["snapshot"]["http_status"], 403)

    def test_intermediate_429_retry_success_is_not_visible_to_pipeline(self):
        self.outcomes = [
            xml_response(), http_error(429, 15), Response(TEX),
            xml_response(OTHER[1]), Response(TEX),
        ]

        report = self.run_pairs([PAIR, OTHER], versions=("v1",), max_attempts=2)

        self.assertFalse(report["stopped"])
        self.assertNotIn("stop_reason", report)
        self.assertEqual(len(report["papers"]), 2)
        self.assertEqual(self.opener.open.call_count, 5)
        self.assertEqual(report["papers"][0]["source"]["v1"]["http_status"], 200)
        self.assertEqual(report["papers"][0]["source"]["v1"]["attempts"], 2)
        self.assertEqual(report["papers"][1]["source"]["v1"]["status"], "success")
        self.assertEqual(self.starts, [0, 3, 18, 21, 24])

    def test_final_429_after_retries_stops_before_following_requests(self):
        self.outcomes = ([xml_response(versions=2)] + [http_error(429) for _ in range(3)] +
                         [Response(TEX), xml_response(OTHER[1]), Response(TEX)])

        report = self.run_pairs([PAIR, OTHER], max_attempts=3)

        self.assertTrue(report["stopped"])
        self.assertEqual(report["stop_reason"]["http_status"], 429)
        self.assertEqual(self.opener.open.call_count, 4)
        self.assertEqual(len(report["papers"]), 1)
        self.assertEqual(report["papers"][0]["source"]["v1"]["attempts"], 3)
        self.assertEqual(self.manifest()["versions"]["v1"]["status"], "rate_limited")
        self.assertEqual(report["papers"][0]["pending"], ["v2"])
        self.assertFalse((self.paper_path() / "v2").exists())
        self.assertFalse(self.paper_path(OTHER).exists())
        self.assertEqual(len(self.outcomes), 3)

    def test_cached_old_rate_limit_does_not_stop_a_selected_valid_version(self):
        store = self.seed(versions=2, completed=True)
        store.begin_version(*PAIR, "v2")
        store.record_source_result(dict(
            mssv=PAIR[0], arxiv_id=PAIR[1], versioned_id=PAIR[1] + "v2",
            status="rate_limited", http_status=429, attempts=1, files=[], error="old fixture 429",
        ))
        before = self.snapshot()

        report = self.run_pairs(versions=("v1",), max_attempts=1)

        self.assertFalse(report["stopped"])
        self.assertNotIn("stop_reason", report)
        self.assertEqual(report["papers"][0]["skipped"], ["v1"])
        self.assertEqual(self.snapshot(), before)
        self.opener.open.assert_not_called()

    def test_storage_validation_failure_on_policy_results_stops_without_more_requests(self):
        for stage, code in (("discovery", 403), ("source", 429)):
            with self.subTest(stage=stage, http_status=code):
                self.root = Path(self.temp.name) / stage
                self.opener.open.reset_mock()
                self.seed(versions=1, completed=True)
                before = self.snapshot()
                self.outcomes = ([http_error(code)] if stage == "discovery" else
                                 [xml_response(OTHER[1], versions=2), http_error(code)])
                self.outcomes.extend([xml_response(THIRD[1]), Response(TEX)])
                method = "record_discovery" if stage == "discovery" else "record_source_result"
                original = getattr(CorpusStorage, method)
                rejected = {}
                def reject(instance, result):
                    if result["arxiv_id"] == OTHER[1] and result.get("http_status") == code:
                        rejected["bytes"] = self.snapshot(OTHER)
                        raise storage.StorageError("fixture rejected policy checkpoint")
                    return original(instance, result)
                with patch.object(CorpusStorage, method, new=reject):
                    report = self.run_pairs([PAIR, OTHER, THIRD], max_attempts=1)

                self.assertTrue(report["stopped"])
                self.assertEqual(report["stop_reason"]["http_status"], code)
                self.assertFalse(report["stop_reason"]["checkpoint_saved"])
                self.assertEqual(report["papers"][1]["errors"][-1]["type"], "StorageError")
                self.assertEqual(self.opener.open.call_count, 1 if stage == "discovery" else 2)
                self.assertEqual(self.snapshot(OTHER), rejected["bytes"])
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.paper_path(THIRD).exists())
                if stage == "source":
                    self.assertEqual(self.manifest(OTHER)["versions"]["v1"]["status"], "in_progress")
                else:
                    self.assertEqual(self.manifest(OTHER)["discovery_status"], "not_attempted")


if __name__ == "__main__":
    unittest.main()
