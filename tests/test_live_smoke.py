"""Controlled smoke tests: no CLI live invocation; HTTP is always fake."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from src import batch, pipeline, storage
import test_pipeline as fx
import test_s2_integration as integration
from test_semantic_scholar import Response, http_error, paper
from test_semantic_scholar_references import page, reference

# Resolve the draft only in the TEMP test launcher; normal runs use repo scripts.
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_smoke.py"
spec = importlib.util.spec_from_file_location("controlled_smoke_under_test", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
PAIR = smoke.DEFAULT_PAIR


class LiveSmokeTests(unittest.TestCase):
    sleep = integration.S2IntegrationTests.sleep
    open_response = integration.S2IntegrationTests.open_response
    s2_open = integration.S2IntegrationTests.s2_open
    close_remaining = integration.S2IntegrationTests.close_remaining
    arxiv_success = integration.S2IntegrationTests.arxiv_success
    s2_success = integration.S2IntegrationTests.s2_success
    store = integration.S2IntegrationTests.store
    paper_path = integration.S2IntegrationTests.paper_path
    snapshot = integration.S2IntegrationTests.snapshot

    def setUp(self):
        integration.S2IntegrationTests.setUp(self)
        self.output = Path(self.temp.name) / "smoke"
        self.root = self.output / "corpus"
        code = Path(pipeline.__file__).resolve().parents[1]
        for item in (patch.object(smoke, "LOCK_PATH", Path(self.temp.name) / "smoke.lock"),
                     patch.object(smoke, "CODE_ROOT", code),
                     patch.object(smoke, "REPORT_ROOT", code.with_name("Arxiv-Project-Group10-Report"))):
            item.start()
            self.addCleanup(item.stop)

    def plan(self, *, live=False, resume=False, extra=()):
        options = smoke.parse_args(["--output", str(self.output), *extra])
        # Exercise the private execution boundary only with fake transport.
        # We never execute a command or main(argv) containing --live.
        options.live, options.resume = live, resume
        return smoke.make_plan(options)

    def execute(self, *, resume=False):
        return smoke._execute_live(self.plan(live=True, resume=resume))

    def success(self, *, versions=3, refs=None):
        self.arxiv_success((PAIR,), versions=versions)
        self.s2_success((PAIR,), refs=refs)

    def files(self):
        return {p.relative_to(self.output).as_posix(): p.read_bytes()
                for p in self.output.rglob("*") if p.is_file()}

    def test_default_dry_run_has_no_writes_or_requests(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(smoke.main([]), 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value["plan"]["pair"], list(PAIR))
        self.assertEqual(value["plan"]["mode"], "dry_run")
        self.assertEqual(value["plan"]["request_bound"], 6)
        self.assertFalse(Path(value["plan"]["output"]).exists())
        self.assertEqual((self.requests, self.s2_requests), ([], []))

    def test_explicit_output_dry_run_does_not_delegate_or_create(self):
        with patch.object(batch, "run_batch", side_effect=AssertionError("Must not execute")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(smoke.main(["--output", str(self.output)]), 0)
        self.assertFalse(self.output.exists())
        self.assertFalse(smoke.LOCK_PATH.exists())

    def test_explicit_live_flag_is_required_but_not_dispatched_in_test(self):
        self.assertFalse(smoke.parse_args([]).live)
        self.assertTrue(smoke.parse_args(["--live"]).live)  # Parse only.
        with self.assertRaises(smoke.SmokeError):
            smoke._execute_live(self.plan())
        self.assertFalse(self.output.exists())

    def test_multiple_ids_and_repeated_id_options_rejected(self):
        for args in (["--arxiv-id", PAIR[1], fx.PAIR[1]],
                     ["--arxiv-id", PAIR[1], "--arxiv-id", fx.PAIR[1]],
                     ["--arxiv-id", PAIR[1] + "," + fx.PAIR[1]]):
            with self.subTest(args=args):
                with contextlib.redirect_stderr(io.StringIO()):
                    if "," in args[-1]:
                        with self.assertRaises(ValueError):
                            self.plan(extra=args)
                    else:
                        with self.assertRaises(SystemExit):
                            smoke.parse_args(args)
        self.assertEqual(self.requests, [])

    def test_wrong_ownership_rejected_before_output(self):
        with self.assertRaises(ValueError):
            self.plan(extra=["--mssv", fx.PAIR[0]])
        self.assertFalse(self.output.exists())

    def test_limits_are_fixed_or_bounded(self):
        plan = self.plan()
        self.assertEqual(plan["versions"], ["v1"])
        self.assertEqual(plan["max_attempts"], 1)
        self.assertEqual(plan["s2_config"]["max_attempts"], 1)
        self.assertEqual(plan["s2_config"]["reference_limit"], 100)
        for extra in (["--max-pages", "4"], ["--max-pages", "0"],
                      ["--timeout", "nan"], ["--timeout", "61"],
                      ["--s2-interval", "0.01"]):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.plan(extra=extra)

    def test_unsafe_output_paths_rejected(self):
        paths = [smoke.CODE_ROOT, smoke.CODE_ROOT / "data" / "new-smoke",
                 smoke.REPORT_ROOT / "new-smoke", smoke.CODE_ROOT.parent,
                 Path(self.temp.name).anchor, "relative-output",
                 str(self.output / ".." / "escape"), str(Path(self.temp.name) / "NUL"),
                 "//server/share/smoke"]
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                smoke.make_plan(smoke.parse_args(["--output", str(path)]))
        self.assertEqual((self.requests, self.s2_requests), ([], []))

    def test_linked_output_ancestor_rejected_without_creating(self):
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda p: p == self.output or original(p)):
            with self.assertRaises(ValueError):
                self.plan()
        self.assertFalse(self.output.exists())

    def test_mapped_network_drive_is_rejected_before_filesystem_probe(self):
        with patch.object(smoke, "_local_drive", return_value=False):
            with patch.object(batch, "_safe", side_effect=AssertionError("Must not probe remote output")):
                with self.assertRaises(smoke.SmokeError):
                    self.plan()

    def test_existing_foreign_output_is_never_overwritten_or_reused(self):
        self.output.mkdir()
        old = self.output / "old-pilot.txt"
        old.write_bytes(b"old corpus evidence")
        before = self.files()
        for resume in (False, True):
            with self.subTest(resume=resume), self.assertRaises((ValueError, OSError)):
                self.plan(live=True, resume=resume)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.requests, [])

    def test_full_fake_pipeline_source_v1_and_s2_are_verified(self):
        self.success()
        report, code = self.execute()
        self.assertEqual(code, 0)
        check = report["verification"]
        self.assertEqual(check["integrity_status"], "verified")
        self.assertEqual(check["discovery"]["status"], "found")
        self.assertTrue(check["source_v1"]["verified"])
        self.assertEqual(check["s2_metadata_snapshot_http_status"], 200)
        self.assertEqual(check["references"]["status"], "complete")
        self.assertFalse(check["aggregation"]["publication_complete"]["all_complete"])
        self.assertEqual(self.store().read_manifest(*PAIR)["versions"]["v2"]["status"], "pending")
        self.assertFalse((self.paper_path(PAIR) / "v2").exists())
        self.assertEqual((len(self.requests), len(self.s2_requests)), (2, 2))
        self.assertEqual(len(self.limiters), 1)
        self.assertFalse(smoke.LOCK_PATH.exists())

    def test_max_three_pages_300_items_remains_partial_with_more(self):
        self.arxiv_success((PAIR,), versions=3)
        raws = [reference()] * 100
        self.s2_outcomes = [
            Response([paper(PAIR[1])]),
            page(raws, next=100, total=350),
            page(raws, offset=100, next=200, total=350),
            page(raws, offset=200, next=300, total=350),
        ]
        report, code = self.execute()
        self.assertEqual((len(self.requests), len(self.s2_requests)), (2, 4))
        self.assertEqual(code, 2)
        refs = report["verification"]["references"]
        self.assertEqual(refs["status"], "partial")
        self.assertEqual(refs["coverage"]["retrieved_count"], 300)
        self.assertEqual(refs["coverage"]["continuation"], {"offset": 300})
        self.assertFalse(refs["coverage"]["complete"])
        self.assertIsNone(report["verification"]["aggregation"]["data_coverage"]["reference_items"])

    def test_403_429_at_each_stage_stops_without_extra_requests(self):
        for stage, status in (("discovery", 403), ("source", 429),
                              ("metadata", 429), ("references", 429)):
            with self.subTest(stage=stage):
                self.output = Path(self.temp.name) / stage
                self.root = self.output / "corpus"
                before = len(self.requests) + len(self.s2_requests)
                if stage == "discovery":
                    self.outcomes = [fx.http_error(status)]
                elif stage == "source":
                    self.outcomes = [fx.xml_response(PAIR[1], 3), fx.http_error(status)]
                else:
                    self.arxiv_success((PAIR,), versions=3)
                    self.s2_outcomes = ([http_error(status)] if stage == "metadata" else [
                        Response([paper(PAIR[1])]), http_error(status)])
                report, code = self.execute()
                self.assertEqual(code, 3)
                self.assertEqual(report["smoke_status"], "stopped")
                self.assertEqual(report["stop_reason"]["http_status"], status)
                expected = {"discovery": 1, "source": 2, "metadata": 3, "references": 4}[stage]
                self.assertEqual(len(self.requests) + len(self.s2_requests) - before, expected)

    def test_save_failure_after_429_keeps_stop_signal(self):
        self.arxiv_success((PAIR,), versions=3)
        self.s2_outcomes = [http_error(429)]
        original = storage.CorpusStorage.save_metadata

        def fail(store, mssv, identifier, value):
            snapshot = (value["provenance"]["semantic_scholar"] or {}).get("snapshot", {})
            if snapshot.get("http_status") == 429:
                raise OSError("fixture write failed")
            return original(store, mssv, identifier, value)

        with patch.object(storage.CorpusStorage, "save_metadata", fail):
            report, code = self.execute()
        self.assertEqual(code, 3)
        self.assertFalse(report["stop_reason"]["checkpoint_saved"])
        self.assertEqual(len(self.s2_requests), 1)
        self.assertTrue(self.store().should_skip_version(*PAIR, "v1"))

    def test_resume_skips_good_source_metadata_and_complete_references(self):
        self.success()
        first, _ = self.execute()
        before = self.snapshot(PAIR)
        counts = len(self.requests), len(self.s2_requests)
        second, code = self.execute(resume=True)
        self.assertEqual(code, 0)
        self.assertEqual((len(self.requests), len(self.s2_requests)), counts)
        for name in ("metadata.json", "references.json", "v1/main.tex"):
            self.assertEqual(self.snapshot(PAIR)[name], before[name])
        self.assertNotEqual(first["report_path"], second["report_path"])
        self.assertTrue(Path(first["report_path"]).is_file())

    def test_partial_references_resume_preserves_prefix_and_no_source_redownload(self):
        self.arxiv_success((PAIR,), versions=3)
        self.s2_outcomes = [Response([paper(PAIR[1])]), page([reference()], next=1), TimeoutError()]
        first, code = self.execute()
        self.assertEqual(code, 2)
        prefix = self.store().read_references(*PAIR)["items"]
        self.s2_outcomes = [page([reference()], next=1), page([reference("2502.08235")], offset=1)]
        second, code = self.execute(resume=True)
        self.assertEqual(code, 0)
        refs = self.store().read_references(*PAIR)
        self.assertEqual(refs["items"][:1], prefix)
        self.assertEqual(len(refs["items"]), 2)
        self.assertEqual(len(self.requests), 2)

    def test_resume_after_rate_limit_is_blocked_for_manual_review(self):
        self.outcomes = [fx.http_error(429)]
        self.execute()
        before = self.files()
        plan = self.plan(live=True, resume=True)
        self.assertTrue(plan["resume_blocked_by_http_policy"])
        with self.assertRaises(smoke.SmokeError):
            smoke._execute_live(plan)
        self.assertEqual(self.files(), before)
        self.assertEqual(len(self.requests), 1)

    def test_dry_resume_only_reads_existing_data(self):
        self.success()
        self.execute()
        before = self.files()
        counts = len(self.requests), len(self.s2_requests)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(smoke.main(["--resume", "--output", str(self.output)]), 0)
        self.assertEqual(json.loads(output.getvalue())["verification"]["integrity_status"], "verified")
        self.assertEqual(self.files(), before)
        self.assertEqual((len(self.requests), len(self.s2_requests)), counts)

    def test_corrupt_source_blocks_resume_without_overwrite(self):
        self.success()
        self.execute()
        source = self.paper_path(PAIR) / "v1/main.tex"
        source.write_bytes(b"corrupt fixture")
        before, counts = self.files(), (len(self.requests), len(self.s2_requests))
        with self.assertRaises(ValueError):
            self.plan(live=True, resume=True)
        self.assertEqual(self.files(), before)
        self.assertEqual((len(self.requests), len(self.s2_requests)), counts)

    def test_wrong_metadata_identity_blocks_resume_before_requests(self):
        self.success()
        self.execute()
        path = self.paper_path(PAIR) / "metadata.json"
        value = json.loads(path.read_text())
        value["provenance"]["semantic_scholar"]["snapshot"]["paper"]["externalIds"]["ArXiv"] = fx.PAIR[1]
        path.write_text(json.dumps(value), encoding="utf-8")
        counts = len(self.requests), len(self.s2_requests)
        before = self.files()
        with self.assertRaises(ValueError):
            self.plan(live=True, resume=True)
        self.assertEqual(self.files(), before)
        self.assertEqual((len(self.requests), len(self.s2_requests)), counts)

    def test_resume_config_and_identity_cannot_change(self):
        self.success()
        self.execute()
        for extra in (["--max-pages", "2"], ["--arxiv-id", fx.PAIR[1], "--mssv", fx.PAIR[0]]):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.plan(live=True, resume=True, extra=extra)

    def test_global_stale_lock_is_preserved_and_prevents_output(self):
        smoke.LOCK_PATH.write_bytes(b"previous process evidence")
        self.success()
        with self.assertRaises(smoke.SmokeError):
            self.execute()
        self.assertEqual(smoke.LOCK_PATH.read_bytes(), b"previous process evidence")
        self.assertFalse(self.output.exists())
        self.assertEqual((self.requests, self.s2_requests), ([], []))

    def test_lock_cleanup_failure_does_not_mask_http_stop(self):
        self.outcomes = [fx.http_error(429)]
        original = Path.unlink
        target = smoke.LOCK_PATH.resolve()

        def deny_lock(path, *args, **kwargs):
            if path.resolve() == target:
                raise PermissionError("fixture cleanup failure")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", deny_lock):
            with contextlib.redirect_stderr(io.StringIO()) as error:
                report, code = self.execute()
        self.assertEqual(code, 3)
        self.assertEqual(report["stop_reason"]["http_status"], 429)
        self.assertIn("lock cleanup failed", error.getvalue())
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(smoke.LOCK_PATH.exists())

    def test_summary_success_without_storage_does_not_pass_verification(self):
        fake = dict(status="finished", stopped=False, stop_reason=None)
        with patch.object(batch, "run_batch", return_value=fake):
            report, code = self.execute()
        self.assertEqual(code, 1)
        self.assertEqual(report["verification"]["integrity_status"], "invalid")
        self.assertEqual(report["smoke_status"], "failed")

    def test_arxiv_not_found_and_s2_not_found_are_distinct(self):
        self.outcomes = [fx.Response(
            b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/">'
            b'<o:totalResults>0</o:totalResults></feed>', "application/atom+xml")]
        report, code = self.execute()
        self.assertEqual(code, 2)
        self.assertEqual(report["verification"]["discovery"]["status"], "not_found")
        self.assertEqual(len(self.s2_requests), 0)

    def test_s2_missing_identity_is_not_relaxed_to_force_success(self):
        self.arxiv_success((PAIR,), versions=3)
        p = paper(PAIR[1])
        p["externalIds"] = {}
        self.s2_outcomes = [Response([p])]
        report, code = self.execute()
        self.assertEqual(code, 2)
        self.assertFalse(report["verification"]["s2_identity_verified"])
        self.assertEqual(report["verification"]["s2_metadata_status"], "invalid_response")
        self.assertEqual(len(self.s2_requests), 1)

    def test_api_key_is_absent_from_plans_reports_and_exception_text(self):
        key = "smoke-fixture-key-never-persist"
        self.success()
        with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": key}):
            with contextlib.redirect_stdout(io.StringIO()) as text:
                smoke.main(["--output", str(self.output)])
            self.assertNotIn(key, text.getvalue())
            report, _ = self.execute()
            self.assertNotIn(key, json.dumps(report))
            for p in self.output.rglob("*.json"):
                self.assertNotIn(key, p.read_text(encoding="utf-8"))

    def test_exception_message_containing_key_is_not_reported(self):
        key = "smoke-exception-secret-fixture"
        with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": key}):
            with patch.object(batch, "run_batch", side_effect=RuntimeError(key)):
                report, code = self.execute()
        self.assertEqual(code, 1)
        self.assertNotIn(key, json.dumps(report))

    def test_s2_not_found_is_not_publication_not_found_or_empty_complete(self):
        self.arxiv_success((PAIR,), versions=3)
        self.s2_outcomes = [Response([None]), http_error(404)]
        report, code = self.execute()
        check = report["verification"]
        self.assertEqual(code, 2)
        self.assertEqual(check["discovery"]["status"], "found")
        self.assertEqual(check["s2_metadata_status"], "not_found")
        self.assertEqual(check["references"]["status"], "not_found")
        self.assertFalse(check["reference_coverage_verified"])
        self.assertIsNone(check["aggregation"]["data_coverage"]["reference_items"])

    def test_no_tex_is_verified_absence_not_successful_tex_download(self):
        self.outcomes = [fx.xml_response(PAIR[1], 3), fx.Response(b"%PDF-1.4\nfixture", "application/pdf")]
        self.s2_success((PAIR,))
        report, code = self.execute()
        self.assertEqual(code, 2)
        self.assertEqual(report["verification"]["source_v1"]["status"], "no_tex_source")
        self.assertTrue(report["verification"]["source_v1"]["verified"])
        self.assertEqual(report["verification"]["source_v1"]["files"], 0)

    def test_metadata_hint_over_300_prevents_false_smoke_success(self):
        self.arxiv_success((PAIR,), versions=1)
        p = paper(PAIR[1])
        p["referenceCount"] = 350
        self.s2_outcomes = [Response([p]), page([reference()] * 100)]
        report, code = self.execute()
        check = report["verification"]
        self.assertEqual(code, 2)
        self.assertEqual(check["references"]["status"], "complete")  # Stored API pagination claim.
        self.assertFalse(check["reference_coverage_verified"])
        self.assertFalse(check["publication_complete_verified"])
        self.assertEqual(check["coverage_issue"], "metadata_reference_count_disagrees")
        counts = len(self.requests), len(self.s2_requests)
        with self.assertRaises(smoke.SmokeError):
            self.plan(live=True, resume=True)
        self.assertEqual((len(self.requests), len(self.s2_requests)), counts)

    def test_argument_error_does_not_echo_accidentally_pasted_secret(self):
        with contextlib.redirect_stderr(io.StringIO()) as error:
            with self.assertRaises(SystemExit):
                smoke.parse_args(["--api-key", "accidental-secret-fixture"])
        self.assertNotIn("accidental-secret-fixture", error.getvalue())


if __name__ == "__main__":
    unittest.main()
