"""Root .env configuration: fake keys, fake transports and temporary files only."""
import contextlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src import local_config, semantic_scholar as s2
import test_semantic_scholar as metadata_fx
from test_semantic_scholar_references import page
from test_live_smoke import smoke
import test_s2_integration as integration


KEY = "SEMANTIC_SCHOLAR_API_KEY"
SECRET = "offline-dotenv-key-only"


def configure_file(test):
    temporary = tempfile.TemporaryDirectory(prefix="group10-env-fixture-")
    test.addCleanup(temporary.cleanup)
    test.config_root = Path(temporary.name)
    test.env_file = test.config_root / ".env"
    for item in (patch.object(local_config, "PROJECT_ROOT", test.config_root),
                 patch.dict(os.environ, {})):
        item.start()
        test.addCleanup(item.stop)
    os.environ.pop(KEY, None)


class LocalConfigTests(unittest.TestCase):
    def setUp(self):
        configure_file(self)

    def write(self, content):
        self.env_file.write_text(content, encoding="utf-8", newline="")

    def test_missing_file_preserves_unkeyed_behavior(self):
        self.assertIsNone(local_config.get_semantic_scholar_api_key())
        self.assertNotIn(KEY, os.environ)
        self.assertFalse(self.env_file.exists())

    def test_only_named_key_is_read_without_mutating_environment(self):
        self.write("OTHER_SETTING=ignored\n" + KEY + "=" + SECRET + "\n")
        before = dict(os.environ)
        self.assertEqual(local_config.get_semantic_scholar_api_key(), SECRET)
        self.assertEqual(dict(os.environ), before)

    def test_single_line_quotes_comments_export_and_bom_crlf(self):
        cases = [
            KEY + "=" + SECRET,
            " # private local settings\n" + KEY + " = " + SECRET + " # comment\n",
            "export " + KEY + "='" + SECRET + "' # comment",
            KEY + '="' + SECRET + '"',
            "\ufeff" + KEY + "=" + SECRET + "\r\n",
        ]
        for content in cases:
            with self.subTest(format=cases.index(content)):
                self.write(content)
                self.assertEqual(local_config.get_semantic_scholar_api_key(), SECRET)

    def test_hash_in_key_is_not_silently_truncated(self):
        self.write(KEY + "=" + SECRET + "#suffix")
        self.assertEqual(local_config.get_semantic_scholar_api_key(), SECRET + "#suffix")

    def test_environment_wins_without_opening_file(self):
        os.environ[KEY] = "environment-fixture-key"
        with patch.object(Path, "open", side_effect=AssertionError("Must not read file")):
            self.assertEqual(local_config.get_semantic_scholar_api_key(), os.environ[KEY])

    def test_explicit_empty_environment_disables_file_fallback(self):
        self.write(KEY + "=" + SECRET)
        for value in ("", "   "):
            os.environ[KEY] = value
            self.assertIsNone(local_config.get_semantic_scholar_api_key())

    def test_invalid_environment_is_not_replaced_by_valid_file(self):
        self.write(KEY + "=" + SECRET)
        for value in ("bad key", "bad\r\nkey", "bad\x00key", "nonascii-\u00e9"):
            with patch.object(os.environ, "get", return_value=value):
                with self.assertRaises(ValueError) as caught:
                    local_config.get_semantic_scholar_api_key()
            self.assertNotIn(value, str(caught.exception))
            self.assertEqual(self.env_file.read_text(), KEY + "=" + SECRET)

    def test_file_without_key_or_empty_placeholder_is_unkeyed(self):
        for content in ("# no key\n", "OTHER_KEY=value\n", KEY + "=\n",
                        KEY + "=''\n", KEY + '=""\n'):
            with self.subTest(case=content):
                self.write(content)
                self.assertIsNone(local_config.get_semantic_scholar_api_key())

    def test_only_explicit_project_root_is_used(self):
        other = self.config_root / "unrelated"
        other.mkdir()
        (other / ".env").write_text(KEY + "=wrong-location-fixture", encoding="utf-8")
        previous = Path.cwd()
        try:
            os.chdir(other)
            self.assertIsNone(local_config.get_semantic_scholar_api_key())
            self.write(KEY + "=" + SECRET)
            self.assertEqual(local_config.get_semantic_scholar_api_key(), SECRET)
        finally:
            os.chdir(previous)

    def test_malformed_file_has_safe_error_and_unchanged_bytes(self):
        cases = [
            KEY + '="' + SECRET,
            KEY + "='" + SECRET + "' garbage",
            KEY + "=" + SECRET + "\n" + KEY + "=duplicate",
            KEY + "=" + SECRET + " with-spaces",
            KEY + "=" + SECRET + "\x00",
            KEY + "=" + SECRET + "\rhidden",
            KEY + "=" + SECRET + "\nmalformed " + SECRET,
            KEY + "=${" + SECRET + "}",
            KEY + "=$(" + SECRET + ")",
            KEY + "=`" + SECRET + "`",
        ]
        for content in cases:
            with self.subTest(case=cases.index(content)):
                self.write(content)
                before = self.env_file.read_bytes()
                logs = io.StringIO()
                handler = logging.StreamHandler(logs)
                logging.getLogger().addHandler(handler)
                try:
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        with self.assertRaises(ValueError) as caught:
                            local_config.get_semantic_scholar_api_key()
                finally:
                    logging.getLogger().removeHandler(handler)
                    handler.close()
                self.assertNotIn(SECRET, str(caught.exception) + logs.getvalue() + output.getvalue())
                self.assertEqual(self.env_file.read_bytes(), before)
                self.assertNotIn(KEY, os.environ)

    def test_invalid_utf8_is_rejected_without_raw_content(self):
        self.env_file.write_bytes((KEY + "=" + SECRET).encode() + b"\xff")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            local_config.get_semantic_scholar_api_key()

    def test_oversized_file_is_rejected(self):
        self.write("#" * (local_config._MAX_ENV_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "size limit"):
            local_config.get_semantic_scholar_api_key()

    def test_unreadable_file_does_not_echo_os_exception(self):
        with patch.object(Path, "open", side_effect=PermissionError(SECRET)):
            with self.assertRaises(ValueError) as caught:
                local_config.get_semantic_scholar_api_key()
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_symlink_is_rejected_before_read(self):
        with patch.object(Path, "is_symlink", return_value=True):
            with patch.object(Path, "open", side_effect=AssertionError("Must not read target")):
                with self.assertRaisesRegex(ValueError, "symbolic link"):
                    local_config.get_semantic_scholar_api_key()

    def test_import_has_no_key_or_file_reads(self):
        spec = importlib.util.spec_from_file_location("isolated_local_config", local_config.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.object(os.environ, "get", side_effect=AssertionError("No key read")), \
             patch.object(Path, "open", side_effect=AssertionError("No file read")):
            spec.loader.exec_module(module)

    def test_changes_are_read_on_next_call_without_stale_process_key(self):
        self.write(KEY + "=" + SECRET)
        self.assertEqual(local_config.get_semantic_scholar_api_key(), SECRET)
        self.write(KEY + "=new-offline-fixture")
        self.assertEqual(local_config.get_semantic_scholar_api_key(), "new-offline-fixture")
        self.assertNotIn(KEY, os.environ)


class LocalEnvClientTests(unittest.TestCase):
    sleep = metadata_fx.SemanticScholarTests.sleep
    open_response = metadata_fx.SemanticScholarTests.open_response
    fetch = metadata_fx.SemanticScholarTests.fetch
    success = metadata_fx.SemanticScholarTests.success

    def setUp(self):
        metadata_fx.SemanticScholarTests.setUp(self)
        configure_file(self)
        self.env_file.write_text(KEY + "=" + SECRET, encoding="utf-8")

    def test_metadata_uses_dotenv_in_existing_header(self):
        self.outcomes = [self.success(metadata_fx.identifiers(1))]
        self.assertEqual(self.fetch()[0]["status"], "found")
        self.assertEqual(self.transport.open.call_args.args[0].get_header("X-api-key"), SECRET)

    def test_references_uses_same_key_and_shared_limiter(self):
        self.outcomes = [self.success(metadata_fx.identifiers(1)), page([])]
        self.fetch()
        snapshot = s2.fetch_references("2502.09992", timeout=15, max_attempts=1)
        self.assertEqual(snapshot["status"], "complete")
        self.assertEqual(len(self.starts), 2)
        self.assertGreaterEqual(self.starts[1] - self.starts[0], self.limiter.min_interval)
        for call in self.transport.open.call_args_list:
            self.assertEqual(call.args[0].get_header("X-api-key"), SECRET)
        self.assertNotIn(SECRET, json.dumps(snapshot))

    def test_dotenv_header_survives_real_urllib_handler_to_http_connection(self):
        # Run the actual opener/HTTPS handler; only the final connection is fake.
        from urllib.request import ProxyHandler, build_opener

        captured = []
        response = metadata_fx.Response([metadata_fx.paper(metadata_fx.identifiers(1)[0])])
        self.addCleanup(response.close)
        response.code, response.reason = 200, "OK"
        response.info = lambda: response.headers

        class Connection:
            def __init__(self, host, timeout, **options):
                self.sock = None
                captured.append({"host": host, "timeout": timeout})

            def set_debuglevel(self, level):
                pass

            def request(self, method, target, body, headers, **options):
                captured[-1].update(method=method, target=target, body=body, headers=headers)

            def getresponse(self):
                return response

            def close(self):
                pass  # No socket or external resource is ever created.

        opener = build_opener(ProxyHandler({}), s2._NoRedirect())
        with patch.object(s2, "_opener", opener), patch("http.client.HTTPSConnection", Connection):
            result = self.fetch(max_attempts=1)
        self.assertEqual(result[0]["status"], "found")
        self.assertEqual(len(captured), 1)
        actual = captured[0]
        self.assertEqual((actual["host"], actual["timeout"], actual["method"]),
                         ("api.semanticscholar.org", 15, "POST"))
        headers = {key.lower(): value for key, value in actual["headers"].items()}
        self.assertEqual(headers["x-api-key"], SECRET)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["host"], "api.semanticscholar.org")
        self.assertEqual(int(headers["content-length"]), len(actual["body"]))
        self.assertEqual(json.loads(actual["body"]), {"ids": ["ARXIV:" + metadata_fx.identifiers(1)[0]]})
        self.assertTrue(actual["target"].startswith("/graph/v1/paper/batch?fields="))
        self.assertTrue(response.closed)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_no_file_keeps_header_absent(self):
        self.env_file.unlink()  # Only this test's temporary fixture.
        self.outcomes = [self.success(metadata_fx.identifiers(1))]
        self.fetch()
        self.assertIsNone(self.transport.open.call_args.args[0].get_header("X-api-key"))

    def test_environment_wins_in_actual_client(self):
        os.environ[KEY] = "preferred-environment-fixture"
        self.outcomes = [self.success(metadata_fx.identifiers(1))]
        self.fetch()
        self.assertEqual(self.transport.open.call_args.args[0].get_header("X-api-key"),
                         "preferred-environment-fixture")

    def test_malformed_key_fails_before_transport_without_echo(self):
        self.env_file.write_text(KEY + "=" + SECRET + " invalid", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.fetch()
        self.assertNotIn(SECRET, str(caught.exception))
        self.transport.open.assert_not_called()

    def test_429_still_stops_chunks_without_leaking_file_key(self):
        self.outcomes = [metadata_fx.http_error(429)]
        result = self.fetch(metadata_fx.identifiers(2), batch_size=1, max_attempts=1)
        self.assertEqual([item["status"] for item in result], ["rate_limited", "rate_limited"])
        self.assertEqual(len(self.starts), 1)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_response_echoing_file_key_is_still_rejected(self):
        response = metadata_fx.paper(metadata_fx.identifiers(1)[0])
        response["title"] = SECRET
        self.outcomes = [metadata_fx.Response([response])]
        result = self.fetch()
        self.assertEqual(result[0]["status"], "invalid_response")
        self.assertNotIn(SECRET, json.dumps(result))


class LocalEnvEntrypointTests(unittest.TestCase):
    sleep = integration.S2IntegrationTests.sleep
    open_response = integration.S2IntegrationTests.open_response
    store = integration.S2IntegrationTests.store
    manifest = integration.S2IntegrationTests.manifest
    paper_path = integration.S2IntegrationTests.paper_path
    snapshot = integration.S2IntegrationTests.snapshot
    seed = integration.S2IntegrationTests.seed
    s2_open = integration.S2IntegrationTests.s2_open
    close_remaining = integration.S2IntegrationTests.close_remaining
    arxiv_success = integration.S2IntegrationTests.arxiv_success
    s2_success = integration.S2IntegrationTests.s2_success
    config = integration.S2IntegrationTests.config
    run_on = integration.S2IntegrationTests.run_on

    def setUp(self):
        integration.S2IntegrationTests.setUp(self)
        configure_file(self)
        self.env_file.write_text(KEY + "=" + SECRET, encoding="utf-8")
        self.output = Path(self.temp.name) / "dry-smoke-output"

    def test_pipeline_resume_uses_cache_with_dotenv_key(self):
        self.arxiv_success()
        self.s2_success()
        self.run_on()
        before = self.snapshot()
        counts = len(self.requests), len(self.s2_requests)
        self.run_on()
        self.assertEqual((len(self.requests), len(self.s2_requests)), counts)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(all(r.get_header("X-api-key") == SECRET for r in self.s2_requests))

    def test_batch_uses_dotenv_without_persisting_secret(self):
        self.arxiv_success()
        self.s2_success()
        settings = integration.S2IntegrationTests.settings(self)
        integration.batch.run_batch([integration.PAIR], self.root, **settings)
        self.assertTrue(all(r.get_header("X-api-key") == SECRET for r in self.s2_requests))
        self.assertEqual(len(self.s2_requests), 2)
        for path in Path(self.temp.name).rglob("*.json"):
            self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))

    def test_benchmark_uses_dotenv_with_existing_foundation_and_safe_report(self):
        self.arxiv_success()
        self.s2_success()

        def foundation(callback, **options):
            callback()
            metrics = dict(status="success", elapsed_seconds=1.0, peak_ram_mb=1)
            Path(options["report_path"]).write_text(json.dumps(metrics), encoding="utf-8")
            return metrics

        with patch.object(integration.benchmark, "benchmark_pipeline", side_effect=foundation):
            report = integration.benchmark_runner.run_benchmark(
                [integration.PAIR], self.root, report_root=Path(self.temp.name) / "reports",
                **integration.S2IntegrationTests.settings(self))
        self.assertEqual(report["batch_status"], "finished")
        self.assertTrue(all(r.get_header("X-api-key") == SECRET for r in self.s2_requests))
        self.assertEqual(len(self.s2_requests), 2)
        self.assertNotIn(SECRET, json.dumps(report))
        for path in Path(self.temp.name).rglob("*.json"):
            self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))

    def _execute_fake_smoke(self):
        # Exercise the real script -> Batch -> Pipeline -> S2 path, never CLI --live.
        self.root = self.output / "corpus"
        options = smoke.parse_args(["--output", str(self.output)])
        options.live = True
        plan = smoke.make_plan(options)
        with patch.object(smoke, "LOCK_PATH", Path(self.temp.name) / "fixture-smoke.lock"):
            return smoke._execute_live(plan)

    def test_full_smoke_dotenv_metadata_request_429_has_key_and_stops(self):
        from urllib.parse import parse_qs, urlsplit

        pair = smoke.DEFAULT_PAIR
        self.arxiv_success((pair,), versions=3)
        self.s2_outcomes = [metadata_fx.http_error(429)]
        report, code = self._execute_fake_smoke()

        self.assertEqual(code, 3)
        self.assertEqual(report["smoke_status"], "stopped")
        self.assertEqual(report["stop_reason"]["stage"], "s2_metadata")
        self.assertTrue(report["stop_reason"]["checkpoint_saved"])
        self.assertEqual(report["verification"]["integrity_status"], "verified")
        self.assertEqual((len(self.requests), len(self.s2_requests)), (2, 1))
        self.assertEqual(len(self.limiters), 1)
        request = self.s2_requests[0]
        parts = urlsplit(request.full_url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path),
                         ("https", "api.semanticscholar.org", "/graph/v1/paper/batch"))
        expected_fields = (
            "paperId,corpusId,externalIds,title,abstract,authors,year,venue,"
            "publicationVenue,publicationTypes,journal,citationCount,"
            "influentialCitationCount,referenceCount,fieldsOfStudy,"
            "s2FieldsOfStudy,openAccessPdf,url")
        self.assertEqual(parse_qs(parts.query), {"fields": [expected_fields]})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"ids": ["ARXIV:" + pair[1]]})
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["x-api-key"], SECRET)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["accept-encoding"], "identity")
        self.assertEqual(headers["user-agent"], s2.USER_AGENT)
        self.assertNotIn(KEY, os.environ)
        self.assertEqual(self.s2_starts[0], self.starts[-1])  # Fresh S2 limiter has no initial delay.
        manifest = self.store().read_manifest(*pair)
        self.assertEqual([manifest["versions"][v]["status"] for v in ("v1", "v2", "v3")],
                         ["success", "pending", "pending"])
        self.assertTrue(self.store().should_skip_version(*pair, "v1"))
        self.assertEqual(self.store().read_references(*pair)["status"], "not_attempted")
        snapshot = self.store().read_metadata(*pair)["provenance"]["semantic_scholar"]["snapshot"]
        self.assertEqual((snapshot["status"], snapshot["http_status"], snapshot["attempts"]),
                         ("rate_limited", 429, 1))
        self.assertNotIn(SECRET, json.dumps(report))
        for path in self.output.rglob("*.json"):
            self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))

    def test_full_smoke_dotenv_metadata_and_references_share_limiter(self):
        pair = smoke.DEFAULT_PAIR
        self.arxiv_success((pair,), versions=3)
        self.s2_success((pair,))
        report, code = self._execute_fake_smoke()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.limiters), 1)
        self.assertEqual(self.limiters[0].min_interval, 2)
        self.assertEqual((len(self.requests), len(self.s2_requests)), (2, 2))
        self.assertGreaterEqual(self.s2_starts[1] - self.s2_starts[0], 2)
        self.assertEqual([request.get_method() for request in self.s2_requests], ["POST", "GET"])
        self.assertTrue(all(request.get_header("X-api-key") == SECRET
                            for request in self.s2_requests))
        self.assertTrue(report["verification"]["reference_coverage_verified"])
        self.assertNotIn(SECRET, json.dumps(report))

    def test_s2_off_does_not_read_even_malformed_dotenv(self):
        self.env_file.write_text(SECRET + " invalid assignment", encoding="utf-8")
        self.arxiv_success()
        self.run_on(s2_config=None)
        self.assertEqual(self.s2_requests, [])

    def test_dry_run_never_calls_batch_or_network_or_writes_output(self):
        with patch.object(smoke.batch, "run_batch", side_effect=AssertionError("No batch")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = smoke.main(["--output", str(self.output)])
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET, output.getvalue())
        self.assertEqual((self.requests, self.s2_requests), ([], []))
        self.assertFalse(self.output.exists())

    def test_dry_run_rejects_key_in_output_path(self):
        secret_path = self.output / SECRET
        with contextlib.redirect_stderr(io.StringIO()) as error, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = smoke.main(["--output", str(secret_path)])
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, output.getvalue() + error.getvalue())
        self.assertFalse(self.output.exists())
        self.assertEqual((self.requests, self.s2_requests), ([], []))

    def test_smoke_redaction_handles_dotenv_without_environment_mutation(self):
        value = smoke._redact({"value": [SECRET, "prefix-" + SECRET]})
        self.assertEqual(value, {"value": ["[REDACTED]", "prefix-[REDACTED]"]})
        self.assertNotIn(KEY, os.environ)

    def test_malformed_file_dry_run_fails_safely_without_requests_or_output(self):
        self.env_file.write_text(KEY + '="' + SECRET, encoding="utf-8")
        before = self.env_file.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()) as error:
            code = smoke.main(["--output", str(self.output)])
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, error.getvalue())
        self.assertEqual((self.requests, self.s2_requests), ([], []))
        self.assertEqual(self.env_file.read_bytes(), before)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
