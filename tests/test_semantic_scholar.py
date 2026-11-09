"""Issue #5 tests: fake transport/clock only; no S2 requests or corpus files."""
import contextlib
from email.message import Message
from email.utils import formatdate
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from src import semantic_scholar as s2


def identifiers(count):
    return [f"2502.{8235 + n:05d}" for n in range(count)]


def paper(identifier, index=0):
    return dict(
        paperId=f"fixture-{index}", corpusId=index, externalIds={"ArXiv": identifier},
        title="Offline fixture", abstract=None, authors=[{"authorId": None, "name": "Fixture"}],
        year=2025, venue=None, publicationVenue=None, publicationTypes=None,
        journal=None, citationCount=0, influentialCitationCount=0, referenceCount=None,
        fieldsOfStudy=None, s2FieldsOfStudy=None, openAccessPdf=None, url=None,
    )


class Response(io.BytesIO):
    def __init__(self, value=None, *, body=None, status=200, headers=None):
        raw = json.dumps(value).encode() if body is None else body
        super().__init__(raw)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json; charset=utf-8"
        self.headers["Content-Length"] = str(len(raw))
        for key, value in (headers or {}).items():
            if self.headers.get(key) is not None:
                self.headers.replace_header(key, value)
            else:
                self.headers[key] = value
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


def http_error(code, after=None, message="fixture HTTP failure"):
    headers = Message()
    if after is not None:
        headers["Retry-After"] = str(after)
    return HTTPError(s2.BATCH_URL, code, message, headers, io.BytesIO())


_DEFAULT_INPUT = object()


class SemanticScholarTests(unittest.TestCase):
    def setUp(self):
        self.now, self.starts = 0.0, []
        self.outcomes, self.responses, self.errors = [], [], []
        self.transport = Mock()
        self.transport.open.side_effect = self.open_response
        self.limiter = s2.RateLimiter()
        patches = [
            patch.object(s2, "_opener", self.transport),
            patch.object(s2, "DEFAULT_LIMITER", self.limiter),
            patch.object(s2.time, "monotonic", lambda: self.now),
            patch.object(s2.time, "time", lambda: 1700000000.0 + self.now),
            patch.object(s2.time, "sleep", self.sleep),
            patch.object(s2.random, "uniform", return_value=0),
            patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": ""}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def sleep(self, seconds):
        self.assertGreaterEqual(seconds, 0)
        self.now += seconds

    def open_response(self, request, timeout):
        self.assertTrue(self.limiter._lock.locked())
        self.assertEqual(timeout, 15)
        self.assertTrue(all(r.closed for r in self.responses))
        self.assertTrue(all(e.fp is None or e.fp.closed for e in self.errors))
        self.starts.append(self.now)
        self.assertTrue(self.outcomes, "Unexpected extra transport attempt")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            if isinstance(outcome, HTTPError):
                self.errors.append(outcome)
            raise outcome
        self.responses.append(outcome)
        return outcome

    def fetch(self, ids=_DEFAULT_INPUT, **options):
        settings = {"timeout": 15}
        settings.update(options)
        return s2.fetch_metadata(
            identifiers(1) if ids is _DEFAULT_INPUT else ids, **settings,
        )

    def success(self, ids):
        return Response([paper(identifier, i) for i, identifier in enumerate(ids)])

    def test_one_id_post_fields_and_standard_result(self):
        ids = identifiers(1)
        self.outcomes = [self.success(ids)]
        results = self.fetch(ids)
        self.assertEqual(len(results), 1)
        self.assertEqual(set(results[0]), {
            "arxiv_id", "status", "http_status", "attempts", "paper", "error",
        })
        self.assertEqual(results[0]["status"], "found")
        self.assertEqual(results[0]["http_status"], 200)
        self.assertEqual(results[0]["attempts"], 1)
        request = self.transport.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(urlsplit(request.full_url).netloc, "api.semanticscholar.org")
        self.assertEqual(json.loads(request.data), {"ids": ["ARXIV:" + ids[0]]})
        expected = {
            "paperId", "corpusId", "externalIds", "title", "abstract", "authors",
            "year", "venue", "publicationVenue", "publicationTypes", "journal",
            "citationCount", "influentialCitationCount", "referenceCount",
            "fieldsOfStudy", "s2FieldsOfStudy", "openAccessPdf", "url",
        }
        self.assertEqual(set(parse_qs(urlsplit(request.full_url).query)["fields"][0].split(",")), expected)

    def test_default_100_ids_single_request(self):
        ids = identifiers(100)
        self.outcomes = [self.success(ids)]
        result = self.fetch(ids)
        self.assertEqual([r["arxiv_id"] for r in result], ids)
        self.assertEqual(self.transport.open.call_count, 1)

    def test_500_ids_single_configured_request(self):
        ids = identifiers(500)
        self.outcomes = [self.success(ids)]
        result = self.fetch(ids, batch_size=500)
        self.assertEqual(len(result), 500)
        self.assertEqual(self.transport.open.call_count, 1)
        self.assertTrue(all(r["status"] == "found" for r in result))

    def test_default_chunking_above_500_preserves_every_input(self):
        ids = identifiers(501)
        self.outcomes = [self.success(ids[n:n + 100]) for n in range(0, len(ids), 100)]
        result = self.fetch(ids)
        self.assertEqual([r["arxiv_id"] for r in result], ids)
        self.assertEqual([len(json.loads(c.args[0].data)["ids"]) for c in self.transport.open.call_args_list],
                         [100, 100, 100, 100, 100, 1])
        self.assertEqual(self.starts, [0, 1, 2, 3, 4, 5])

    def test_500_chunking_above_1000(self):
        ids = identifiers(1001)
        self.outcomes = [self.success(ids[n:n + 500]) for n in range(0, len(ids), 500)]
        result = self.fetch(ids, batch_size=500)
        self.assertEqual(len(result), len(ids))
        self.assertEqual([r["arxiv_id"] for r in result], ids)
        self.assertEqual(self.transport.open.call_count, 3)

    def test_mixed_ownership_kept_by_position_without_guessing_mssv(self):
        pairs = [
            ("24120232", "2502.09992"), ("24120010", "2508.07872"),
            ("24120035", "2507.16659"), ("24120260", "2501.02642"),
        ]
        ids = [identifier for _, identifier in pairs]
        self.outcomes = [self.success(ids[:2]), self.success(ids[2:])]
        result = self.fetch(ids, batch_size=2)
        self.assertEqual([(owner, r["arxiv_id"]) for (owner, _), r in zip(pairs, result)], pairs)
        self.assertTrue(all("mssv" not in r for r in result))

    def test_null_is_not_found_in_its_original_slot(self):
        ids = identifiers(3)
        self.outcomes = [Response([paper(ids[0]), None, paper(ids[2])])]
        result = self.fetch(ids)
        self.assertEqual([r["status"] for r in result], ["found", "not_found", "found"])
        self.assertEqual(result[1], dict(
            arxiv_id=ids[1], status="not_found", http_status=200,
            attempts=1, paper=None, error=None,
        ))

    def test_duplicate_ids_preserved(self):
        ids = identifiers(1) * 2
        self.outcomes = [self.success(ids)]
        result = self.fetch(ids)
        self.assertEqual(len(result), 2)
        self.assertEqual([r["arxiv_id"] for r in result], ids)
        self.assertEqual(json.loads(self.transport.open.call_args.args[0].data)["ids"], ["ARXIV:" + ids[0]] * 2)

    def test_generic_old_and_new_style_base_ids(self):
        ids = ["2502.09992", "1401.1234", "hep-th/9901001"]
        self.outcomes = [self.success(ids)]
        self.assertEqual([r["arxiv_id"] for r in self.fetch(ids)], ids)

    def test_429_then_success_respects_retry_after(self):
        self.outcomes = [http_error(429, 7), self.success(identifiers(1))]
        result = self.fetch()
        self.assertEqual(result[0]["status"], "found")
        self.assertEqual(result[0]["attempts"], 2)
        self.assertEqual(self.starts, [0, 7])

    def test_5xx_retries_exponential_backoff_and_jitter(self):
        self.outcomes = [http_error(503), http_error(502), self.success(identifiers(1))]
        with patch.object(s2.random, "uniform", return_value=.5):
            result = self.fetch()
        self.assertEqual(result[0]["attempts"], 3)
        self.assertEqual(self.starts, [0, 1.5, 4.0])

    def test_nonretryable_4xx_never_retry_or_become_not_found(self):
        for code in (400, 401, 403, 404, 413, 422):
            with self.subTest(code=code):
                self.outcomes = [http_error(code)]
                result = self.fetch()
                self.assertEqual(result[0]["status"], "http_error")
                self.assertEqual(result[0]["http_status"], code)
                self.assertEqual(result[0]["attempts"], 1)
                if code == 413:
                    self.assertEqual(result[0]["error"], "http_413_request_too_large")
        self.assertEqual(self.transport.open.call_count, 6)

    def test_timeout_retries_then_reports_timeout(self):
        self.outcomes = [TimeoutError("private detail")] * 2
        result = self.fetch(max_attempts=2)
        self.assertEqual(result[0]["status"], "timeout")
        self.assertEqual(result[0]["attempts"], 2)
        self.assertIsNone(result[0]["http_status"])
        self.assertNotIn("private detail", json.dumps(result))

    def test_network_timeout_wrapped_and_tls_no_retry(self):
        self.outcomes = [URLError(TimeoutError("secret")), self.success(identifiers(1))]
        self.assertEqual(self.fetch()[0]["attempts"], 2)
        self.outcomes = [URLError(ssl.SSLError("secret"))]
        result = self.fetch()
        self.assertEqual(result[0]["status"], "http_error")
        self.assertEqual(result[0]["attempts"], 1)
        self.assertEqual(result[0]["error"], "network_error")

    def test_malformed_json_is_not_retried(self):
        self.outcomes = [Response(body=b"{bad json")]
        result = self.fetch()
        self.assertEqual(result[0]["status"], "invalid_response")
        self.assertEqual(result[0]["error"], "invalid_json")
        self.assertEqual(result[0]["attempts"], 1)

    def test_incomplete_extra_or_wrong_top_structure_quarantines_whole_chunk(self):
        ids = identifiers(2)
        for value in ([paper(ids[0])], [paper(ids[0]), None, None], {}, None, "bad"):
            with self.subTest(value=value):
                self.outcomes = [Response(value)]
                result = self.fetch(ids)
                self.assertEqual([r["status"] for r in result], ["invalid_response"] * 2)
                self.assertTrue(all(r["paper"] is None for r in result))
                self.assertEqual([r["arxiv_id"] for r in result], ids)

    def test_reordered_external_arxiv_identity_is_rejected_not_remapped(self):
        ids = identifiers(2)
        self.outcomes = [Response([paper(ids[1]), paper(ids[0])])]
        result = self.fetch(ids)
        self.assertTrue(all(r["status"] == "invalid_response" for r in result))
        self.assertTrue(all(r["error"] == "arxiv_identity_mismatch" for r in result))
        self.assertTrue(all(r["paper"] is None for r in result))

    def test_bad_paper_in_middle_quarantines_valid_prefix(self):
        ids = identifiers(3)
        self.outcomes = [Response([paper(ids[0]), {"title": "missing paperId"}, paper(ids[2])])]
        result = self.fetch(ids)
        self.assertTrue(all(r["status"] == "invalid_response" for r in result))

    def test_missing_optional_metadata_is_not_fabricated(self):
        record = {"paperId": "fixture", "externalIds": {"ArXiv": identifiers(1)[0]}}
        self.outcomes = [Response([record])]
        result = self.fetch()
        self.assertEqual(result[0]["status"], "found")
        self.assertEqual(result[0]["paper"], record)
        self.assertNotIn("title", result[0]["paper"])
        self.assertNotIn("submission_date", result[0]["paper"])

    def test_null_or_versioned_external_arxiv_id(self):
        ids = identifiers(1)
        for external in (None, {"ArXiv": None}, {"ArXiv": ids[0] + "v3"}):
            with self.subTest(external=external):
                record = paper(ids[0])
                record["externalIds"] = external
                self.outcomes = [Response([record])]
                result = self.fetch()[0]
                self.assertEqual(result["status"], "found" if external and external["ArXiv"] else "invalid_response")
                if result["status"] == "invalid_response":
                    self.assertEqual(result["error"], "arxiv_identity_unverified")

    def test_invalid_field_types_are_not_accepted_as_found(self):
        for field, value in (("corpusId", True), ("authors", ["name"]), ("abstract", {}),
                             ("externalIds", []), ("year", "2025"), ("fieldsOfStudy", [1]),
                             ("referenceCount", -1), ("paperId", None)):
            with self.subTest(field=field):
                record = paper(identifiers(1)[0])
                record[field] = value
                self.outcomes = [Response([record])]
                self.assertEqual(self.fetch()[0]["status"], "invalid_response")

    def test_api_key_present_and_absent(self):
        for key in ("", "fixture-key-only"):
            with self.subTest(key=key):
                self.outcomes = [self.success(identifiers(1))]
                with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": key}):
                    result = self.fetch()
                headers = {k.lower(): v for k, v in self.transport.open.call_args.args[0].header_items()}
                self.assertEqual(headers.get("x-api-key"), key or None)
                self.assertNotIn("fixture-key-only", json.dumps(result))

    def test_error_messages_and_logs_do_not_contain_key_or_response_body(self):
        secret = "fixture-key-must-stay-private"
        output, logs = io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        for outcome in (http_error(403, message=secret), URLError(secret),
                        RuntimeError(secret), Response(body=secret.encode())):
            self.outcomes = [outcome]
            with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": secret}), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = self.fetch(max_attempts=1)
            self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, logs.getvalue() + output.getvalue())

    def test_invalid_key_header_is_rejected_without_echoing_it(self):
        for secret in ("fixture\r\nsecret", "fixture secret", "fixture\x00secret"):
            with self.subTest(secret=repr(secret)):
                with patch.object(os.environ, "get", return_value=secret):
                    with self.assertRaises(ValueError) as caught:
                        self.fetch()
                self.assertNotIn(secret, str(caught.exception))
        self.transport.open.assert_not_called()

    def test_rate_limit_exhaustion_stops_all_remaining_chunks(self):
        ids = identifiers(201)
        self.outcomes = [self.success(ids[:100]), http_error(429, 10), http_error(429, 10)]
        result = self.fetch(ids, max_attempts=2)
        self.assertEqual(len(result), len(ids))
        self.assertEqual([r["arxiv_id"] for r in result], ids)
        self.assertTrue(all(r["status"] == "found" for r in result[:100]))
        self.assertTrue(all(r["status"] == "rate_limited" and r["http_status"] == 429
                            and r["attempts"] == 2 for r in result[100:200]))
        self.assertEqual(result[200], dict(
            arxiv_id=ids[200], status="rate_limited", http_status=None, attempts=0,
            paper=None, error="not_attempted_after_rate_limit",
        ))
        self.assertEqual(self.transport.open.call_count, 3)
        self.assertEqual(self.starts, [0, 1, 11])

    def test_exhausted_429_cooldown_is_shared_with_next_explicit_call(self):
        self.outcomes = [http_error(429, 8)]
        result = self.fetch(max_attempts=1)
        self.assertEqual(result[0]["status"], "rate_limited")
        self.assertEqual(self.now, 0)
        self.outcomes = [self.success(identifiers(1))]
        self.fetch(max_attempts=1)
        self.assertEqual(self.starts, [0, 8])

    def test_default_limiter_shared_across_calls_and_other_s2_consumer_slot(self):
        self.outcomes = [self.success(identifiers(1)), self.success(identifiers(1))]
        self.fetch()
        with s2.DEFAULT_LIMITER.request_slot() as postpone:
            self.assertEqual(self.now, 1)
            postpone(3)
        self.fetch()
        self.assertEqual(self.starts, [0, 4])

    def test_custom_pacing_and_slow_response_completion(self):
        self.limiter = s2.RateLimiter(2.5)
        self.outcomes = [self.success(identifiers(1)), self.success(identifiers(1))]
        self.fetch(limiter=self.limiter)
        self.now += 1.0
        self.fetch(limiter=self.limiter)
        self.assertEqual(self.starts, [0, 2.5])

    def test_retry_after_http_date_and_invalid_headers(self):
        date = formatdate(1700000000 + 9, usegmt=True)
        self.outcomes = [http_error(429, date), self.success(identifiers(1))]
        self.fetch()
        self.assertEqual(self.starts, [0, 9])
        for invalid in ("nonsense", "-3", "nan", "inf", "1.5"):
            self.assertEqual(s2._retry_after(invalid), 0)

    def test_retry_after_success_applies_to_next_chunk(self):
        ids = identifiers(2)
        response = self.success(ids[:1])
        response.headers["Retry-After"] = "6"
        self.outcomes = [response, self.success(ids[1:])]
        self.fetch(ids, batch_size=1)
        self.assertEqual(self.starts, [0, 6])

    def test_response_size_guard_does_not_auto_split_or_retry(self):
        ids = identifiers(2)
        response = Response(body=b"x" * 100)
        self.outcomes = [response]
        result = self.fetch(ids, max_response_bytes=30)
        self.assertTrue(all(r["status"] == "invalid_response" for r in result))
        self.assertTrue(all(r["error"] == "response_too_large" for r in result))
        self.assertEqual(response.read_sizes, [])
        self.assertEqual(self.transport.open.call_count, 1)

    def test_actual_read_is_bounded_even_without_content_length(self):
        response = Response(body=b"x" * 100)
        del response.headers["Content-Length"]
        self.outcomes = [response]
        result = self.fetch(max_response_bytes=30)
        self.assertEqual(result[0]["error"], "response_too_large")
        self.assertEqual(response.read_sizes, [31])

    def test_size_rejection_keeps_chunk_results_and_later_independent_ids(self):
        ids = identifiers(3)
        self.outcomes = [http_error(413), self.success(ids[2:])]
        result = self.fetch(ids, batch_size=2)
        self.assertEqual([r["status"] for r in result], ["http_error", "http_error", "found"])
        self.assertEqual(self.transport.open.call_count, 2)

    def test_bad_lengths_media_and_encoding_are_invalid_response(self):
        for headers in ({"Content-Length": "invalid"}, {"Content-Length": "1"},
                        {"Content-Type": "text/html"}, {"Content-Encoding": "gzip"}):
            with self.subTest(headers=headers):
                self.outcomes = [Response([paper(identifiers(1)[0])], headers=headers)]
                self.assertEqual(self.fetch()[0]["status"], "invalid_response")

    def test_duplicate_json_keys_and_nonfinite_values_rejected(self):
        for raw in (b'[{"paperId":"a","paperId":"b"}]', b'[{"paperId":"a","year":NaN}]'):
            with self.subTest(raw=raw):
                self.outcomes = [Response(body=raw)]
                self.assertEqual(self.fetch()[0]["error"], "invalid_json")

    def test_redirect_never_followed_with_key(self):
        request = Request(s2.BATCH_URL, data=b"{}", headers={"x-api-key": "fixture-secret"}, method="POST")
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(s2._NoRedirect().redirect_request(
                request, None, code, "redirect", {}, "https://untrusted.invalid/",
            ))
            self.outcomes = [Response(status=code)]
            with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": "fixture-secret"}):
                result = self.fetch()
            self.assertEqual(result[0]["status"], "http_error")
            self.assertEqual(result[0]["attempts"], 1)
        self.assertEqual(self.transport.open.call_count, 5)

    def test_invalid_inputs_checked_even_after_first_chunk_before_http(self):
        invalid = [None, "2502.09992", iter(identifiers(1)), [None], ["ARXIV:2502.09992"],
                   ["2502.09992v1"], ["../../outside"], ["2500.12345"], identifiers(101) + ["bad"]]
        for ids in invalid:
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):
                    self.fetch(ids)
        self.transport.open.assert_not_called()

    def test_invalid_configuration_before_http(self):
        for options in (dict(batch_size=0), dict(batch_size=501), dict(batch_size=True),
                        dict(timeout=0), dict(timeout=float("nan")), dict(timeout=True),
                        dict(max_attempts=0), dict(max_attempts=True), dict(limiter=object()),
                        dict(max_response_bytes=0), dict(max_response_bytes=10_000_001)):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.fetch(**options)
        self.transport.open.assert_not_called()
        for interval in (0, -1, True, float("inf")):
            with self.assertRaises(ValueError):
                s2.RateLimiter(interval)

    def test_empty_input_is_noop(self):
        self.assertEqual(self.fetch([]), [])
        self.transport.open.assert_not_called()

    def test_import_does_not_call_api_or_read_key(self):
        module_path = Path(s2.__file__)
        spec = importlib.util.spec_from_file_location("offline_s2_import", module_path)
        isolated = importlib.util.module_from_spec(spec)
        opener = Mock()
        opener.open.side_effect = AssertionError("No HTTP on import")
        with patch("urllib.request.build_opener", return_value=opener), \
             patch.object(os.environ, "get", side_effect=AssertionError("No key read on import")):
            spec.loader.exec_module(isolated)
        opener.open.assert_not_called()


    def test_two_threads_share_one_limiter_and_connection(self):
        import threading
        ids = identifiers(2)
        results, failures, active = [], [], []
        gate = threading.Barrier(2)
        def transport(request, timeout):
            self.assertTrue(self.limiter._lock.locked())
            self.assertFalse(active)
            active.append(True)
            self.starts.append(self.now)
            requested = json.loads(request.data)["ids"][0].removeprefix("ARXIV:")
            response = self.success([requested])
            active.pop()
            return response
        def worker(identifier):
            try:
                gate.wait(timeout=3)
                results.extend(self.fetch([identifier]))
            except BaseException as exc:
                failures.append(type(exc).__name__)
        self.transport.open.side_effect = transport
        threads = [threading.Thread(target=worker, args=(identifier,)) for identifier in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(failures, [])
        self.assertEqual({r["arxiv_id"] for r in results}, set(ids))
        self.assertTrue(all(r["status"] == "found" for r in results))
        self.assertEqual(self.starts, [0, 1])

    def test_max_attempts_one_means_one_transport_call(self):
        for outcome, expected in ((http_error(503), "http_error"),
                                  (http_error(429), "rate_limited"),
                                  (TimeoutError(), "timeout")):
            with self.subTest(expected=expected):
                previous = self.transport.open.call_count
                self.outcomes = [outcome]
                result = self.fetch(max_attempts=1)
                self.assertEqual(result[0]["status"], expected)
                self.assertEqual(result[0]["attempts"], 1)
                self.assertEqual(self.transport.open.call_count, previous + 1)

    def test_close_failure_does_not_turn_partial_response_into_found(self):
        class BrokenClose(Response):
            def __exit__(self, *args):
                super().__exit__(*args)
                raise OSError("private key text")
        self.outcomes = [BrokenClose([paper(identifiers(1)[0])])]
        result = self.fetch(max_attempts=1)
        self.assertEqual(result[0]["status"], "http_error")
        self.assertIsNone(result[0]["paper"])
        self.assertNotIn("private key text", json.dumps(result))

    def test_invalid_http_status_is_unknown_not_echoed(self):
        self.outcomes = [Response(status="private key text")]
        result = self.fetch(max_attempts=1)
        self.assertEqual(result[0]["status"], "invalid_response")
        self.assertIsNone(result[0]["http_status"])
        self.assertNotIn("private key text", json.dumps(result))

    def test_chunk_integrity_failure_does_not_shift_later_independent_chunk(self):
        ids = identifiers(3)
        self.outcomes = [Response([paper(ids[0])]), self.success(ids[2:])]
        result = self.fetch(ids, batch_size=2)
        self.assertEqual([r["arxiv_id"] for r in result], ids)
        self.assertEqual([r["status"] for r in result], ["invalid_response", "invalid_response", "found"])
        self.assertEqual(result[-1]["paper"]["externalIds"]["ArXiv"], ids[-1])

    def test_completion_pacing_includes_response_read_time(self):
        first = self.success(identifiers(1))
        original_read = first.read
        def slow_read(size):
            self.now += 3.0
            return original_read(size)
        first.read = slow_read
        self.outcomes = [first, self.success(identifiers(1))]
        self.fetch()
        self.fetch()
        self.assertEqual(self.starts, [0, 4])

    def test_author_missing_name_allowed_but_bad_types_not_fabricated(self):
        for author, status in (({}, "found"), ({"name": None}, "found"), ({"name": []}, "invalid_response")):
            with self.subTest(author=author):
                record = paper(identifiers(1)[0])
                record["authors"] = [author]
                self.outcomes = [Response([record])]
                result = self.fetch()
                self.assertEqual(result[0]["status"], status)
                if status == "found":
                    self.assertEqual(result[0]["paper"]["authors"], [author])

    def test_strict_identity_rejects_missing_external_id_by_default(self):
        for external in (None, {}, {"ArXiv": None}):
            with self.subTest(external=external):
                record = paper(identifiers(1)[0])
                record["externalIds"] = external
                self.outcomes = [Response([record])]
                result = self.fetch()
                self.assertEqual(result[0]["status"], "invalid_response")
                self.assertEqual(result[0]["error"], "arxiv_identity_unverified")
                self.assertIsNone(result[0]["paper"])

    def test_explicit_positional_opt_in_preserves_unverified_record(self):
        self.outcomes = [Response([{"paperId": "unverified-paper"}])]
        result = self.fetch(require_arxiv_identity=False)
        self.assertEqual(result[0]["status"], "found")
        self.assertEqual(result[0]["paper"], {"paperId": "unverified-paper"})
        self.assertEqual(set(result[0]), {"arxiv_id", "status", "http_status", "attempts", "paper", "error"})

    def test_positional_opt_in_still_rejects_contradictory_external_identity(self):
        ids = identifiers(2)
        self.outcomes = [Response([paper(ids[1]), paper(ids[0])])]
        result = self.fetch(ids, require_arxiv_identity=False)
        self.assertTrue(all(r["status"] == "invalid_response" for r in result))
        self.assertTrue(all(r["error"] == "arxiv_identity_mismatch" for r in result))

    def test_missing_identity_in_mixed_chunk_quarantines_whole_chunk(self):
        ids = identifiers(3)
        self.outcomes = [Response([paper(ids[0]), {"paperId": "unknown"}, paper(ids[2])])]
        result = self.fetch(ids)
        self.assertEqual([r["arxiv_id"] for r in result], ids)
        self.assertTrue(all(r["status"] == "invalid_response" and r["paper"] is None for r in result))

    def test_reordered_missing_id_records_are_not_silently_assigned(self):
        ids = identifiers(2)
        self.outcomes = [Response([{"paperId": "paper-B"}, {"paperId": "paper-A"}])]
        result = self.fetch(ids)
        self.assertTrue(all(r["error"] == "arxiv_identity_unverified" for r in result))

    def test_strict_identity_null_result_still_not_found(self):
        self.outcomes = [Response([None])]
        self.assertEqual(self.fetch()[0]["status"], "not_found")

    def test_missing_response_element_remains_error_in_strict_mode(self):
        ids = identifiers(2)
        self.outcomes = [Response([paper(ids[0])])]
        result = self.fetch(ids)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(r["error"] == "invalid_batch_length_or_structure" for r in result))

    def test_identity_policy_validation_before_http(self):
        for value in (None, 0, 1, "true", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.fetch(require_arxiv_identity=value)
        self.transport.open.assert_not_called()

    def test_429_close_failure_never_allows_next_chunk(self):
        class BrokenClose(HTTPError):
            def close(self):
                super().close()
                raise OSError("fixture-private-close")
        ids = identifiers(2)
        self.outcomes = [BrokenClose(s2.BATCH_URL, 429, "fixture", {}, io.BytesIO())]
        result = self.fetch(ids, batch_size=1, max_attempts=3)
        self.assertEqual(self.transport.open.call_count, 1)
        self.assertEqual(result[0]["status"], "rate_limited")
        self.assertEqual(result[0]["http_status"], 429)
        self.assertEqual(result[0]["error"], "http_429")
        self.assertEqual(result[1]["attempts"], 0)
        self.assertNotIn("fixture-private-close", json.dumps(result))

    def test_observed_429_survives_response_context_cleanup_failure(self):
        class BrokenResponse(Response):
            def __exit__(self, *args):
                super().__exit__(*args)
                raise OSError("fixture-private-cleanup")
        self.outcomes = [BrokenResponse(status=429)]
        result = self.fetch(max_attempts=3)
        self.assertEqual(result[0]["status"], "rate_limited")
        self.assertEqual(result[0]["http_status"], 429)
        self.assertEqual(result[0]["attempts"], 1)
        self.assertEqual(self.transport.open.call_count, 1)

    def test_413_does_not_split_retry_or_lose_later_ids(self):
        ids = identifiers(3)
        self.outcomes = [http_error(413), self.success(ids[2:])]
        result = self.fetch(ids, batch_size=2)
        self.assertEqual([r["arxiv_id"] for r in result], ids)
        self.assertEqual([r["status"] for r in result], ["http_error", "http_error", "found"])
        self.assertEqual(self.transport.open.call_count, 2)

    def test_oversize_chunk_does_not_repeat_or_shift_subsequent_chunk(self):
        ids = identifiers(3)
        self.outcomes = [Response(body=b"x" * 30), self.success(ids[2:])]
        # The valid fixture needs more than 30 bytes, so the cap applies to both chunks.
        result = self.fetch(ids, batch_size=2, max_response_bytes=20)
        self.assertEqual([r["arxiv_id"] for r in result], ids)
        self.assertEqual(self.transport.open.call_count, 2)
        self.assertTrue(all(r["status"] == "invalid_response" and r["attempts"] == 1 for r in result))

    def assert_status_exception_cleanup(self, *, response_cleanup_fails):
        created, closed_before_response_exit = [], []
        owner = self
        class TrackedHTTPError(HTTPError):
            def __init__(self, *args):
                super().__init__(*args)
                created.append(self)
                # Keep test fixtures owned even if a regression assertion fails.
                owner.addCleanup(self.close)
        class StatusResponse(Response):
            def __exit__(self, *args):
                closed_before_response_exit.append(created[-1].closed)
                super().__exit__(*args)
                if response_cleanup_fails:
                    raise OSError("fixture response cleanup failed")
        for code in (403, 429):
            with self.subTest(code=code):
                response = StatusResponse(status=code)
                self.outcomes = [response]
                before_errors = len(created)
                before_requests = self.transport.open.call_count
                with patch.object(s2, "HTTPError", TrackedHTTPError):
                    result = self.fetch(max_attempts=1)
                self.assertEqual(len(created), before_errors + 1)
                self.assertTrue(closed_before_response_exit[-1])
                self.assertTrue(created[-1].closed)
                self.assertTrue(response.closed)
                self.assertEqual(result[0]["status"], "rate_limited" if code == 429 else "http_error")
                self.assertEqual(result[0]["http_status"], code)
                self.assertEqual(result[0]["attempts"], 1)
                self.assertEqual(self.transport.open.call_count, before_requests + 1)

    def test_synthetic_http_error_closed_before_response_context_exits(self):
        self.assert_status_exception_cleanup(response_cleanup_fails=False)

    def test_synthetic_http_error_closed_when_response_cleanup_raises(self):
        self.assert_status_exception_cleanup(response_cleanup_fails=True)

    def test_http_error_from_transport_closes_its_owned_stream(self):
        for code in (403, 429):
            with self.subTest(code=code):
                body = io.BytesIO(b"offline error body")
                failure = HTTPError(s2.BATCH_URL, code, "fixture", {}, body)
                self.addCleanup(failure.close)
                self.outcomes = [failure]
                with patch.object(failure, "close", wraps=failure.close) as close:
                    result = self.fetch(max_attempts=1)
                    close.assert_called_once()
                self.assertTrue(failure.closed)
                self.assertTrue(body.closed)
                self.assertEqual(result[0]["status"], "rate_limited" if code == 429 else "http_error")


class RateLimiterTests(unittest.TestCase):
    def test_rolling_window_100_requests_per_300_seconds(self):
        from src.semantic_scholar import RateLimiter
        clock = [0.0]
        slept = []
        def fake_monotonic():
            return clock[0]
        def fake_sleep(secs):
            slept.append(secs)
            clock[0] += secs

        limiter = RateLimiter(
            min_interval=1.0,
            window_seconds=300.0,
            max_requests_per_window=5,  # test với giới hạn nhỏ 5 req / 300s
            monotonic_fn=fake_monotonic,
            sleep_fn=fake_sleep,
        )
        # Thực hiện 5 request đầu tiên, mỗi request cách nhau 1s
        for _ in range(5):
            limiter.wait()
            clock[0] += 1.0

        # Request thứ 6 tại t=5.0: Phải chờ đến t=300.0 để request thứ 1 rời khỏi cửa sổ 300s!
        limiter.wait()
        self.assertGreaterEqual(clock[0], 300.0)


if __name__ == "__main__":
    unittest.main()
