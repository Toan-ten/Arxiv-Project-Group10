"""Kiểm thử ngoại tuyến các trang tài liệu tham khảo từ Semantic Scholar."""
import contextlib
import copy
import io
import json
import logging
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit

from src import semantic_scholar as s2
from src.storage import CorpusStorage
from test_semantic_scholar import Response, http_error, identifiers, paper


ARXIV = "2502.09992"


def reference(identifier="hep-th/9901001", **changes):
    value = dict(
        contexts=["Offline context"], intents=["methodology"], isInfluential=False,
        citedPaper=dict(
            paperId="cited-paper-id", corpusId=123, title="Offline cited title",
            authors=[{"authorId": "author-id", "name": "Author One"}],
            year=1999, venue="Fixture venue",
            publicationVenue={"id": "venue-id", "name": "Fixture"},
            publicationDate="1999-02-03",
            externalIds={"ArXiv": identifier, "DOI": "fixture-doi"}, url="https://example.invalid/",
        ),
    )
    value["citedPaper"].update(changes)
    return value


def page(data, offset=0, **changes):
    return Response(dict(offset=offset, data=data, **changes))


class ReferencesTests(unittest.TestCase):
    def setUp(self):
        self.now, self.starts, self.outcomes = 0.0, [], []
        self.responses, self.errors = [], []
        self.limiter = s2.RateLimiter()
        self.transport = Mock()
        self.transport.open.side_effect = self.open_response
        patches = [
            patch.object(s2, "_opener", self.transport),
            patch.object(s2, "DEFAULT_LIMITER", self.limiter),
            patch.object(s2.time, "monotonic", lambda: self.now),
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
        self.assertEqual(timeout, 15)
        self.assertTrue(self.limiter._lock.locked())
        self.assertTrue(all(r.closed for r in self.responses))
        self.assertTrue(all(e.fp is None or e.fp.closed for e in self.errors))
        self.starts.append(self.now)
        self.assertTrue(self.outcomes, "Unexpected extra HTTP attempt")
        value = self.outcomes.pop(0)
        if isinstance(value, BaseException):
            if hasattr(value, "fp"):
                self.errors.append(value)
            raise value
        self.responses.append(value)
        return value

    def fetch(self, identifier=ARXIV, **options):
        options.setdefault("timeout", 15)
        return s2.fetch_references(identifier, **options)

    def offsets(self):
        return [int(parse_qs(urlsplit(c.args[0].full_url).query)["offset"][0])
                for c in self.transport.open.call_args_list]

    def assert_contract(self, value):
        # Exercise the actual read-only v2 validator without creating a corpus.
        validator = object.__new__(CorpusStorage)
        validator.paper_path = Mock(return_value=None)
        validator._references(value, "24120232", value["arxiv_id"])
        json.dumps(value, allow_nan=False)

    def test_one_page_schema_normalization_and_get_fields(self):
        raw = reference()
        self.outcomes = [page([raw])]
        result = self.fetch()
        self.assert_contract(result)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"], dict(complete=True, continuation=None,
                         reported_total=None, retrieved_count=1))
        self.assertIsNone(result["error"])
        request = self.transport.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(unquote(urlsplit(request.full_url).path), "/graph/v1/paper/ARXIV:" + ARXIV + "/references")
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(query["offset"], ["0"])
        self.assertEqual(query["limit"], ["100"])
        self.assertEqual(set(query["fields"][0].split(",")), {
            "paperId", "corpusId", "title", "authors", "year", "venue",
            "publicationVenue", "publicationDate", "externalIds", "url",
            "contexts", "intents", "isInfluential",
        })
        self.assertEqual(result["items"][0]["authors"], ["Author One"])
        self.assertEqual(result["items"][0]["raw"], raw)

    def test_raw_only_corpus_id_type_does_not_reject_reference_page(self):
        raw = reference()
        raw["citedPaper"]["corpusId"] = "corpus-123"
        self.outcomes = [page([raw])]

        result = self.fetch()

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["items"][0]["raw"], raw)
        self.assert_contract(result)

    def test_multiple_pages_follow_next_and_preserve_order(self):
        raws = [reference("2502.08235"), reference("2508.07872"), reference("hep-th/9901001")]
        self.outcomes = [page(raws[:2], next=2), page(raws[2:], offset=2)]
        result = self.fetch(limit=2)
        self.assertEqual(self.offsets(), [0, 2])
        self.assertEqual([i["raw"] for i in result["items"]], raws)
        self.assertTrue(result["coverage"]["complete"])
        self.assert_contract(result)

    def test_valid_empty_references_are_complete(self):
        self.outcomes = [page([])]
        result = self.fetch()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["coverage"]["complete"])
        self.assertIsNotNone(result["provenance"]["retrieved_at"])
        self.assert_contract(result)

    def test_explicit_null_next_is_terminal(self):
        self.outcomes = [page([reference()], next=None)]
        self.assertEqual(self.fetch()["status"], "complete")
        self.assertEqual(self.transport.open.call_count, 1)

    def test_full_page_without_next_finishes_without_speculative_request(self):
        self.outcomes = [page([reference()])]
        self.assertEqual(self.fetch(limit=1)["status"], "complete")
        self.assertEqual(self.transport.open.call_count, 1)

    def test_404_first_page_is_not_found_not_empty_complete(self):
        self.outcomes = [http_error(404)]
        result = self.fetch()
        self.assertEqual(result["status"], "not_found")
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["error"]["http_status"], 404)
        self.assertEqual(result["error"]["attempts"], 1)
        self.assertIsNone(result["coverage"]["continuation"])
        self.assert_contract(result)

    def test_404_later_keeps_previous_items_as_partial(self):
        self.outcomes = [page([reference()], next=1), http_error(404)]
        result = self.fetch()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["coverage"]["continuation"], {"offset": 1})
        self.assertEqual(result["error"]["http_status"], 404)
        self.assert_contract(result)

    def test_429_first_page_exhausts_bounded_attempts_and_stops(self):
        self.outcomes = [http_error(429, 4), http_error(429, 4)]
        result = self.fetch(max_attempts=2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], dict(type="rate_limited", message="http_429",
                         http_status=429, attempts=2, offset=0))
        self.assertEqual(self.starts, [0, 4])
        self.assertEqual(self.offsets(), [0, 0])
        self.assertFalse(result["coverage"]["complete"])
        self.assert_contract(result)

    def test_429_second_page_keeps_previous_raw_and_does_not_request_more(self):
        raw = reference()
        self.outcomes = [page([raw], next=1), http_error(429)]
        result = self.fetch(max_attempts=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["items"][0]["raw"], raw)
        self.assertEqual(result["coverage"]["continuation"], {"offset": 1})
        self.assertEqual(result["error"]["type"], "rate_limited")
        self.assertEqual(self.offsets(), [0, 1])
        self.assert_contract(result)

    def test_timeout_between_pages_is_partial(self):
        self.outcomes = [page([reference()], next=1), TimeoutError("private text")]
        result = self.fetch(max_attempts=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error"]["type"], "timeout")
        self.assertEqual(result["coverage"]["retrieved_count"], 1)
        self.assertEqual(result["coverage"]["continuation"], {"offset": 1})
        self.assertNotIn("private text", json.dumps(result))

    def test_5xx_retries_same_page_then_success(self):
        self.outcomes = [page([reference()], next=1), http_error(503), page([reference()], offset=1)]
        result = self.fetch(max_attempts=2)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(self.offsets(), [0, 1, 1])
        self.assertEqual(self.starts, [0, 1, 2])
        self.assert_contract(result)

    def test_5xx_exhaustion_does_not_claim_zero_references(self):
        self.outcomes = [http_error(503), http_error(502)]
        result = self.fetch(max_attempts=2)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["error"]["http_status"], 502)
        self.assertEqual(result["error"]["attempts"], 2)

    def test_looping_next_keeps_items_without_third_request(self):
        self.outcomes = [page([reference()], next=1), page([reference()], offset=1, next=1)]
        result = self.fetch()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["items"]), 2)
        self.assertIsNone(result["coverage"]["continuation"])
        self.assertEqual(result["error"]["message"], "pagination_not_advancing")
        self.assertEqual(self.offsets(), [0, 1])
        self.assert_contract(result)

    def test_next_wrong_types_and_nonadvancing_are_incomplete(self):
        for value in (True, False, "1", 1.5, {}, [], 0, -1):
            with self.subTest(value=value):
                self.outcomes = [page([reference()], next=value)]
                result = self.fetch()
                self.assertEqual(result["status"], "partial")
                self.assertFalse(result["coverage"]["complete"])
                self.assertEqual(len(result["items"]), 1)
                self.assertIsNone(result["coverage"]["continuation"])
                self.assert_contract(result)

    def test_gap_next_is_rejected_not_used_to_skip_references(self):
        self.outcomes = [page([reference()], next=2)]
        result = self.fetch()
        self.assertEqual(result["error"]["message"], "pagination_gap_or_overlap")
        self.assertEqual(self.offsets(), [0])

    def test_max_pages_keeps_valid_continuation(self):
        self.outcomes = [page([reference()], next=1)]
        result = self.fetch(max_pages=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["continuation"], {"offset": 1})
        self.assertEqual(result["error"], dict(type="pagination_limit",
                         message="max_pages_reached", http_status=None, attempts=0, offset=1))
        self.assert_contract(result)

    def test_terminal_page_at_max_pages_is_complete(self):
        self.outcomes = [page([reference()])]
        self.assertEqual(self.fetch(max_pages=1)["status"], "complete")

    def test_malformed_json_first_page_failed(self):
        self.outcomes = [Response(body=b"{invalid")]
        result = self.fetch()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["type"], "invalid_response")
        self.assertEqual(result["items"], [])
        self.assert_contract(result)

    def test_malformed_later_page_keeps_previous_snapshot_items(self):
        self.outcomes = [page([reference()], next=1), Response(body=b"{bad")]
        result = self.fetch()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["coverage"]["continuation"], {"offset": 1})

    def test_wrong_envelope_offsets_or_data_rejected(self):
        for value in ({}, [], None, {"data": []}, {"data": [], "offset": False},
                      {"data": [], "offset": 2}, {"data": {}, "offset": 0}):
            with self.subTest(value=value):
                self.outcomes = [Response(value)]
                result = self.fetch()
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["coverage"]["complete"])
                self.assert_contract(result)

    def test_cited_paper_null_or_missing_kept_as_unknown_item(self):
        raws = [{"citedPaper": None, "contexts": ["context"]}, {"intents": ["background"]}]
        self.outcomes = [page(raws)]
        result = self.fetch()
        self.assertEqual(len(result["items"]), 2)
        for item, raw in zip(result["items"], raws):
            self.assertTrue(all(item[f] is None for f in s2._REFERENCE_ITEM_FIELDS))
            self.assertEqual(set(item["missing_fields"]), set(s2._REFERENCE_ITEM_FIELDS))
            self.assertEqual(item["raw"], raw)
        self.assert_contract(result)

    def test_missing_arxiv_id_is_preserved_without_filtering(self):
        raw = reference(externalIds={"DOI": "fixture"})
        self.outcomes = [page([raw])]
        item = self.fetch()["items"][0]
        self.assertIsNone(item["arxiv_id"])
        self.assertIn("arxiv_id", item["missing_fields"])
        self.assertEqual(item["raw"], raw)

    def test_old_style_query_and_reference_id_are_not_folder_keys(self):
        raw = reference("hep-th/9901001")
        self.outcomes = [page([raw])]
        result = self.fetch("hep-th/9901001")
        request = self.transport.open.call_args.args[0]
        self.assertIn("ARXIV:hep-th%2F9901001/references", request.full_url)
        self.assertEqual(result["arxiv_id"], "hep-th/9901001")
        self.assertEqual(result["items"][0]["arxiv_id"], "hep-th/9901001")
        self.assertEqual(result["provenance"]["query_identifier"], "ARXIV:hep-th/9901001")
        self.assert_contract(result)

    def test_missing_title_authors_year_are_null_not_defaults(self):
        self.outcomes = [page([{"citedPaper": {"paperId": "known"}}])]
        item = self.fetch()["items"][0]
        for field in ("title", "authors", "year"):
            self.assertIsNone(item[field])
            self.assertIn(field, item["missing_fields"])

    def test_publication_date_is_preserved_but_submission_is_unknown(self):
        self.outcomes = [page([reference()])]
        item = self.fetch()["items"][0]
        self.assertEqual(item["publication_date"], "1999-02-03")
        self.assertIsNone(item["submission_date"])
        self.assertIn("submission_date", item["missing_fields"])

    def test_year_does_not_become_date_and_raw_submission_is_not_trusted(self):
        raw = reference(publicationDate=None, submission_date="1999-01-01")
        self.outcomes = [page([raw])]
        item = self.fetch()["items"][0]
        self.assertEqual(item["year"], 1999)
        self.assertIsNone(item["publication_date"])
        self.assertIsNone(item["submission_date"])
        self.assertEqual(item["raw"], raw)

    def test_paper_id_is_not_corpus_id_or_query_provenance(self):
        self.outcomes = [page([reference()])]
        result = self.fetch()
        self.assertEqual(result["items"][0]["s2_paper_id"], "cited-paper-id")
        self.assertIsNone(result["provenance"]["s2_paper_id"])
        self.assertEqual(self.transport.open.call_count, 1)

    def test_relation_fields_unknown_nested_metadata_and_duplicates_kept_raw(self):
        raw = reference()
        raw["extra"] = {"nested": [1, None, {"fixture": "value"}]}
        raw["citedPaper"]["extra"] = ["unknown metadata"]
        self.outcomes = [page([raw, raw], next=2), page([raw], offset=2)]
        result = self.fetch()
        self.assertEqual([i["raw"] for i in result["items"]], [raw, raw, raw])
        self.assertFalse(result["items"][0]["raw"]["isInfluential"])
        self.assertEqual(result["items"][0]["raw"]["contexts"], ["Offline context"])
        self.assertEqual(result["items"][0]["raw"]["intents"], ["methodology"])
        self.assertIsNot(result["items"][0]["raw"], raw)
        self.assert_contract(result)

    def test_partial_authors_keep_known_names_and_flag_incompleteness(self):
        raw = reference(authors=[{"name": "Known"}, {}, {"name": None}])
        self.outcomes = [page([raw])]
        item = self.fetch()["items"][0]
        self.assertEqual(item["authors"], ["Known"])
        self.assertIn("authors", item["missing_fields"])
        self.assertEqual(item["raw"], raw)

    def test_unknown_authors_are_not_misrepresented_as_known_empty_list(self):
        self.outcomes = [page([reference(authors=[{}])])]
        item = self.fetch()["items"][0]
        self.assertIsNone(item["authors"])
        self.assertIn("authors", item["missing_fields"])

    def test_explicit_empty_authors_preserved_and_marked_missing(self):
        self.outcomes = [page([reference(authors=[])])]
        item = self.fetch()["items"][0]
        self.assertEqual(item["authors"], [])
        self.assertIn("authors", item["missing_fields"])

    def test_api_total_only_when_consistent_and_explicit(self):
        self.outcomes = [page([reference()], next=1, total=2), page([reference()], offset=1, total=2)]
        result = self.fetch()
        self.assertEqual(result["coverage"]["reported_total"], 2)
        self.assertEqual(result["status"], "complete")
        self.assert_contract(result)

    def test_invalid_or_inconsistent_totals_never_claim_complete(self):
        for total in (True, -1, "1", 0, 2):
            with self.subTest(total=total):
                self.outcomes = [page([reference()], total=total)]
                result = self.fetch()
                self.assertEqual(result["status"], "partial")
                self.assertFalse(result["coverage"]["complete"])
                self.assertEqual(len(result["items"]), 1)
                self.assert_contract(result)
        self.outcomes = [page([reference()], next=1, total=2), page([reference()], offset=1, total=3)]
        result = self.fetch()
        self.assertEqual(result["error"]["message"], "invalid_reported_total")
        self.assert_contract(result)

    def test_total_exceeded_retains_items_but_drops_untrustworthy_count(self):
        self.outcomes = [page([reference()], next=1, total=2), page([reference(), reference()], offset=1)]
        result = self.fetch()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["items"]), 3)
        self.assertIsNone(result["coverage"]["reported_total"])
        self.assert_contract(result)

    def test_empty_page_with_next_is_not_complete_or_infinite_loop(self):
        self.outcomes = [page([], next=1)]
        result = self.fetch()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(self.transport.open.call_count, 1)

    def test_page_size_and_invalid_item_types_rejected_atomically(self):
        for value in ([reference(), reference()], [None], [{"citedPaper": []}],
                      [reference(authors="bad")], [reference(year=True)],
                      [reference(publicationDate="1999")]):
            with self.subTest(value=value):
                self.outcomes = [page(value)]
                result = self.fetch(limit=1)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["items"], [])
                self.assert_contract(result)

    def test_limit_response_bytes_is_shared_with_metadata_http(self):
        self.outcomes = [Response(body=b"x" * 50)]
        result = self.fetch(max_response_bytes=10)
        self.assertEqual(result["error"]["message"], "response_too_large")
        self.assertFalse(result["coverage"]["complete"])

    def test_custom_fields_and_limit(self):
        self.outcomes = [page([{"citedPaper": {"paperId": "id"}}])]
        result = self.fetch(fields="paperId,title", limit=1000)
        query = parse_qs(urlsplit(self.transport.open.call_args.args[0].full_url).query)
        self.assertEqual(query["fields"], ["paperId,title"])
        self.assertEqual(query["limit"], ["1000"])
        self.assertIsNone(result["items"][0]["authors"])

    def test_bad_input_or_configuration_before_request(self):
        for identifier in (None, "ARXIV:2502.09992", "2502.09992v1", "../x", "2500.00001"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.fetch(identifier)
        for options in (dict(fields=["citedPaper.title"]), dict(fields=["unknown"]),
                        dict(fields=[]), dict(fields=["title", "title"]), dict(fields=[{}]),
                        dict(limit=0), dict(limit=1001), dict(limit=True),
                        dict(max_pages=0), dict(max_pages=True), dict(timeout=0),
                        dict(max_attempts=0), dict(limiter=object()), dict(max_response_bytes=0)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.fetch(**options)
        self.transport.open.assert_not_called()

    def test_shared_default_limiter_between_metadata_and_references(self):
        self.outcomes = [Response([paper(identifiers(1)[0])]), page([]), Response([paper(identifiers(1)[0])])]
        s2.fetch_metadata(identifiers(1), timeout=15)
        self.fetch()
        s2.fetch_metadata(identifiers(1), timeout=15)
        self.assertEqual(self.starts, [0, 1, 2])

    def test_custom_shared_limiter_and_rate_limit_cooldown(self):
        self.limiter = s2.RateLimiter(2.5)
        self.outcomes = [http_error(429, 8), Response([paper(identifiers(1)[0])])]
        self.fetch(limiter=self.limiter, max_attempts=1)
        s2.fetch_metadata(identifiers(1), timeout=15, limiter=self.limiter)
        self.assertEqual(self.starts, [0, 8])

    def test_optional_key_and_fixed_error_messages_do_not_leak_key(self):
        secret = "fixture-private-key"
        output, logs = io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        for key in ("", secret):
            self.outcomes = [http_error(403, message=secret)]
            with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": key}), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = self.fetch(max_attempts=1)
            headers = {k.lower():v for k,v in self.transport.open.call_args.args[0].header_items()}
            self.assertEqual(headers.get("x-api-key"), key or None)
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(logs.getvalue() + output.getvalue(), "")

    def test_error_snapshot_does_not_mutate_previous_good_snapshot(self):
        self.outcomes = [page([reference()])]
        good = self.fetch()
        before = copy.deepcopy(good)
        self.outcomes = [http_error(429)]
        bad = self.fetch(max_attempts=1)
        self.assertEqual(good, before)
        self.assertEqual(good["status"], "complete")
        self.assertEqual(bad["status"], "failed")

    def test_nonretryable_http_and_network_errors_stay_distinct(self):
        from urllib.error import URLError
        for outcome, kind in ((http_error(400), "http_error"), (http_error(403), "http_error"),
                              (URLError("private detail"), "http_error")):
            self.outcomes = [outcome]
            result = self.fetch(max_attempts=1)
            self.assertEqual(result["error"]["type"], kind)
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("private detail", json.dumps(result))

    def test_short_page_with_valid_next_is_not_prematurely_complete(self):
        self.outcomes = [page([reference()], next=1), page([], offset=1)]
        result = self.fetch(limit=100)
        self.assertEqual(self.offsets(), [0, 1])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"]["retrieved_count"], 1)

    def test_response_echoing_key_is_rejected_without_redacting_raw_silently(self):
        secret = "fixture-secret-never-in-snapshot"
        raw = reference()
        raw["extra"] = {"nested": [secret]}
        self.outcomes = [page([raw])]
        with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": secret}):
            result = self.fetch()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["message"], "response_contains_secret")
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["items"], [])
        self.assertEqual(raw["extra"]["nested"], [secret])

    def test_tainted_second_page_preserves_first_safe_page(self):
        secret = "fixture-secret-never-in-snapshot"
        tainted = reference(title=secret)
        self.outcomes = [page([reference()], next=1), page([tainted], offset=1)]
        with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": secret}):
            result = self.fetch()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["items"]), 1)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["coverage"]["continuation"], {"offset": 1})

    def test_malformed_relationship_fields_do_not_silently_disappear(self):
        for field, value in (("contexts", "bad"), ("intents", [1]), ("isInfluential", "false")):
            with self.subTest(field=field):
                raw = reference()
                raw[field] = value
                self.outcomes = [page([raw])]
                result = self.fetch()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["message"], "invalid_reference_relation")

    def test_invalid_arxiv_string_preserved_raw_without_claiming_valid_id(self):
        raw = reference("not-an-arxiv-id")
        self.outcomes = [page([raw])]
        item = self.fetch()["items"][0]
        self.assertIsNone(item["arxiv_id"])
        self.assertIn("arxiv_id", item["missing_fields"])
        self.assertEqual(item["raw"], raw)

    def test_versioned_external_arxiv_id_preserved_exactly(self):
        self.outcomes = [page([reference("hep-th/9901001v2")])]
        self.assertEqual(self.fetch()["items"][0]["arxiv_id"], "hep-th/9901001v2")

    def test_http_error_close_failure_does_not_leak_exception_secret(self):
        from urllib.error import HTTPError
        class BrokenClose(HTTPError):
            def close(self):
                super().close()
                raise OSError("fixture-secret-close")
        failure = BrokenClose(s2.BATCH_URL, 403, "fixture HTTP failure", {}, io.BytesIO())
        self.outcomes = [failure]
        result = self.fetch(max_attempts=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["message"], "transport_error")
        self.assertNotIn("fixture-secret-close", json.dumps(result))

    def test_numeric_overflow_in_raw_json_cannot_escape_contract_serialization(self):
        self.outcomes = [Response(body=b'{"offset":0,"data":[{"citedPaper":null,"extra":1e999}]}')]
        result = self.fetch()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["message"], "invalid_json")
        self.assert_contract(result)

    def make_partial(self, raws=None):
        raws = [reference()] if raws is None else raws
        self.outcomes = [page(raws, next=len(raws))]
        return self.fetch(max_pages=1)

    def test_429_close_failure_preserves_rate_limit_error_and_previous_items(self):
        from urllib.error import HTTPError
        class BrokenClose(HTTPError):
            def close(self):
                super().close()
                raise OSError("private-close")
        self.outcomes = [page([reference()], next=1),
                         BrokenClose(s2.BATCH_URL, 429, "fixture", {}, io.BytesIO())]
        result = self.fetch(max_attempts=3)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error"]["type"], "rate_limited")
        self.assertEqual(result["error"]["http_status"], 429)
        self.assertEqual(result["coverage"]["retrieved_count"], 1)
        self.assertEqual(self.offsets(), [0, 1])

    def test_invalid_total_cannot_leave_a_trusted_continuation(self):
        for value in (0, -1, True, "2"):
            with self.subTest(value=value):
                self.outcomes = [page([reference()], next=1, total=value)]
                result = self.fetch()
                self.assertEqual(result["status"], "partial")
                self.assertIsNone(result["coverage"]["continuation"])
                self.assertFalse(result["coverage"]["complete"])
                self.assertEqual(len(result["items"]), 1)

    def test_resume_restarts_and_revalidates_prefix_without_duplication(self):
        first, second = reference(), reference("2508.07872")
        old = self.make_partial([first])
        before = copy.deepcopy(old)
        self.outcomes = [page([first], next=1), page([second], offset=1)]
        result = self.fetch(resume_from=old)
        self.assertEqual(self.offsets(), [0, 0, 1])
        self.assertEqual([i["raw"] for i in result["items"]], [first, second])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(old, before)
        self.assert_contract(result)

    def test_resume_uses_fresh_snapshot_without_appending_saved_duplicates(self):
        raw = reference()
        old = self.make_partial([raw, raw])
        self.outcomes = [page([raw], next=1), page([raw], offset=1, next=2),
                         page([raw], offset=2)]
        result = self.fetch(resume_from=old, limit=1)
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual([i["raw"] for i in result["items"]], [raw, raw, raw])
        self.assert_contract(result)

    def test_resume_can_change_page_limit_without_changing_reference_order(self):
        raws = [reference(), reference("2502.08235"), reference("2508.07872")]
        old = self.make_partial(raws[:2])
        self.outcomes = [page(raws)]
        result = self.fetch(resume_from=old, limit=3)
        self.assertEqual([i["raw"] for i in result["items"]], raws)
        self.assertEqual(result["status"], "complete")

    def test_resume_early_timeout_keeps_entire_saved_prefix(self):
        old = self.make_partial([reference(), reference("2508.07872")])
        self.outcomes = [TimeoutError("private")]
        result = self.fetch(resume_from=old, max_attempts=1)
        self.assertEqual(result["items"], old["items"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error"]["type"], "timeout")
        self.assertEqual(result["coverage"]["continuation"], {"offset": 2})
        self.assert_contract(result)

    def test_resume_429_before_prefix_verified_preserves_stop_signal(self):
        old = self.make_partial()
        self.outcomes = [http_error(429)]
        result = self.fetch(resume_from=old, max_attempts=1)
        self.assertEqual(result["items"], old["items"])
        self.assertEqual(result["error"]["type"], "rate_limited")
        self.assertEqual(result["error"]["http_status"], 429)
        self.assertEqual(result["status"], "partial")

    def test_resume_prefix_mismatch_keeps_original_data_and_blocks_continuation(self):
        old = self.make_partial()
        before = copy.deepcopy(old)
        self.outcomes = [page([reference(title="Changed upstream")])]
        result = self.fetch(resume_from=old)
        self.assertEqual(result["items"], old["items"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error"]["message"], "resume_prefix_mismatch")
        self.assertIsNone(result["coverage"]["continuation"])
        self.assertEqual(old, before)
        self.assert_contract(result)

    def test_resume_mismatch_does_not_replace_final_429_diagnostic(self):
        old = self.make_partial()
        self.outcomes = [page([reference(title="Changed")], next=1), http_error(429)]
        result = self.fetch(resume_from=old, max_attempts=1)
        self.assertEqual(result["items"], old["items"])
        self.assertEqual(result["error"]["type"], "rate_limited")
        self.assertEqual(result["error"]["http_status"], 429)
        self.assertEqual(result["error"]["resume_reason"], "resume_prefix_mismatch")
        self.assertIsNone(result["coverage"]["continuation"])

    def test_resume_shortened_complete_result_cannot_erase_saved_items(self):
        old = self.make_partial([reference(), reference("2508.07872")])
        self.outcomes = [page([reference()])]
        result = self.fetch(resume_from=old)
        self.assertEqual(result["error"]["message"], "resume_prefix_truncated")
        self.assertEqual(result["items"], old["items"])
        self.assertFalse(result["coverage"]["complete"])
        self.assertIsNone(result["coverage"]["continuation"])

    def test_resume_404_does_not_turn_existing_data_into_not_found(self):
        old = self.make_partial()
        self.outcomes = [http_error(404)]
        result = self.fetch(resume_from=old)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["items"], old["items"])
        self.assertEqual(result["error"]["http_status"], 404)
        self.assertEqual(result["error"]["resume_reason"], "resume_source_not_found")
        self.assertIsNone(result["coverage"]["continuation"])
        self.assert_contract(result)

    def test_resume_matching_prefix_and_later_error_keeps_new_progress(self):
        first, second = reference(), reference("2508.07872")
        old = self.make_partial()
        self.outcomes = [page([first, second], next=2), TimeoutError()]
        result = self.fetch(resume_from=old, max_attempts=1)
        self.assertEqual([i["raw"] for i in result["items"]], [first, second])
        self.assertEqual(result["coverage"]["continuation"], {"offset": 2})
        self.assertEqual(result["status"], "partial")

    def test_resume_with_invalid_identity_or_normalization_makes_no_http_request(self):
        old = self.make_partial()
        invalid = []
        for path, value in [
            (("contract_version",), True), (("arxiv_id",), "2508.07872"),
            (("status",), "complete"), (("status",), {}),
            (("provenance", "provider"), "other"),
            (("provenance", "query_identifier"), "ARXIV:2508.07872"),
            (("provenance", "s2_paper_id"), "unverified-parent"),
            (("provenance", "retrieved_at"), None),
            (("provenance", "retrieved_at"), "2026-11-06"),
            (("coverage", "complete"), True),
            (("coverage", "retrieved_count"), 2),
            (("coverage", "retrieved_count"), True),
            (("coverage", "reported_total"), 0),
            (("coverage", "continuation"), None),
            (("coverage", "continuation"), {"offset": True}),
            (("coverage", "continuation"), {"offset": -1}),
            (("coverage", "continuation"), {"offset": 2}),
            (("coverage", "continuation"), {"offset": 1, "token": "bad"}),
        ]:
            value_old = copy.deepcopy(old)
            target = value_old
            for field in path[:-1]:
                target = target[field]
            target[path[-1]] = value
            invalid.append(value_old)
        tampered = copy.deepcopy(old)
        tampered["items"][0]["title"] = "Not from raw"
        invalid.append(tampered)
        for snapshot in invalid:
            previous = self.transport.open.call_count
            original = copy.deepcopy(snapshot)
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                self.fetch(resume_from=snapshot)
            self.assertEqual(self.transport.open.call_count, previous)
            self.assertEqual(snapshot, original)

    def test_resume_invalid_snapshot_types_are_rejected_before_http(self):
        for value in ([], "offset=1", 1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.fetch(resume_from=value)
        self.transport.open.assert_not_called()

    def test_resume_input_with_key_is_rejected_without_echoing_secret(self):
        old = self.make_partial()
        secret = "fixture-resume-secret"
        old["error"]["message"] = secret
        previous = self.transport.open.call_count
        with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": secret}):
            with self.assertRaises(ValueError) as caught:
                self.fetch(resume_from=old)
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(self.transport.open.call_count, previous)

    def test_resume_respects_max_pages_and_shared_pacing(self):
        old = self.make_partial([reference(), reference("2508.07872")])
        self.outcomes = [page([reference()], next=1)]
        result = self.fetch(resume_from=old, max_pages=1)
        self.assertEqual(result["items"], old["items"])
        self.assertEqual(result["error"]["type"], "pagination_limit")
        self.assertEqual(result["coverage"]["continuation"], {"offset": 2})
        self.assertEqual(self.starts, [0, 1])
        self.assert_contract(result)

    def test_resume_duplicate_ids_keep_raw_order_across_revalidation(self):
        raws = [reference(), reference(), {"citedPaper": None}]
        old = self.make_partial(raws[:2])
        self.outcomes = [page(raws)]
        result = self.fetch(resume_from=old)
        self.assertEqual([i["raw"] for i in result["items"]], raws)
        self.assertEqual(result["coverage"]["retrieved_count"], 3)

    def test_resume_failed_empty_snapshot_can_become_legitimate_not_found(self):
        self.outcomes = [TimeoutError()]
        old = self.fetch(max_attempts=1)
        self.outcomes = [http_error(404)]
        result = self.fetch(resume_from=old)
        self.assertEqual(result["status"], "not_found")
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["items"], [])

    def test_preserved_resume_data_keeps_original_provenance(self):
        old = self.make_partial()
        old["provenance"]["retrieved_at"] = "2026-11-01T00:00:00Z"
        original = copy.deepcopy(old["provenance"])
        self.outcomes = [TimeoutError()]
        result = self.fetch(resume_from=old, max_attempts=1)
        self.assertEqual(result["provenance"], original)
        self.assertNotEqual(result["error"]["observed_at"], original["retrieved_at"])
        self.assertEqual(result["items"], old["items"])
        self.assert_contract(result)

    def test_partial_replay_shorter_than_saved_prefix_does_not_erase_provenance(self):
        old = self.make_partial([reference(), reference("2508.07872")])
        old["provenance"]["retrieved_at"] = "2026-11-01T00:00:00Z"
        self.outcomes = [page([reference()], next=1), TimeoutError()]
        result = self.fetch(resume_from=old, max_attempts=1)
        self.assertEqual(result["items"], old["items"])
        self.assertEqual(result["provenance"], old["provenance"])
        self.assertEqual(result["coverage"]["continuation"], {"offset": 2})
        self.assertEqual(result["error"]["offset"], 1)
        self.assert_contract(result)


if __name__ == "__main__":
    unittest.main()
