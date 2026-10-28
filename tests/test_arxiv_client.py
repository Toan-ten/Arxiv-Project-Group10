import io
import socket
import ssl
import unittest
from contextlib import ExitStack
from email.message import Message
from email.utils import formatdate
from http.client import IncompleteRead
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request

from src import arxiv_client as client
from src.id_ranges import iter_assigned_ids


MSSV = "24120232"
ARXIV_ID = "2502.08235"


def paper_xml(arxiv_id=ARXIV_ID, version="v3"):
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        f'<id>http://arxiv.org/abs/{arxiv_id}{version}</id>'
        '<title>Sample paper</title><author><name>Example Author</name></author>'
        '<published>2025-02-12T09:00:00Z</published>'
        '<updated>2025-03-01T10:00:00Z</updated></entry></feed>'
    ).encode("utf-8")


def headers(**values):
    result = Message()
    for name, value in values.items():
        result[name.replace("_", "-")] = value
    return result


def http_error(code, retry_after=None):
    values = {} if retry_after is None else {"Retry_After": retry_after}
    return HTTPError(client.API_URL, code, "Test HTTP error", headers(**values), io.BytesIO())


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def wall_time(self):
        return 1700000000.0 + self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeResponse:
    def __init__(self, body=None, content_type="application/atom+xml; charset=utf-8",
                 status=200, read_error=None, duration=0):
        self.body = paper_xml() if body is None else body
        self.status = status
        self.headers = headers()
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        self.read_error = read_error
        self.duration = duration
        self.closed = False
        self.clock = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self):
        self.clock.now += self.duration
        if self.read_error is not None:
            raise self.read_error
        return self.body


class ArxivClientTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.outcomes = []
        self.starts = []
        self.responses = []
        self.error_streams = []
        self.original_opener = client._opener
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(client, "_next_request_at", 0.0))
        self.stack.enter_context(patch.object(client.time, "monotonic", self.clock.monotonic))
        self.stack.enter_context(patch.object(client.time, "time", self.clock.wall_time))
        self.sleep = self.stack.enter_context(
            patch.object(client.time, "sleep", side_effect=self.clock.sleep)
        )
        self.jitter = self.stack.enter_context(patch.object(client.random, "uniform", return_value=0.0))
        self.opener = Mock()
        self.opener.open.side_effect = self.open_response
        self.stack.enter_context(patch.object(client, "_opener", self.opener))

    def open_response(self, request, timeout):
        self.assertTrue(client._request_lock.locked())
        self.assertTrue(all(response.closed for response in self.responses))
        self.assertTrue(all(stream.closed for stream in self.error_streams))
        self.starts.append(self.clock.now)
        self.assertTrue(self.outcomes, "Unexpected extra HTTP attempt")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            if isinstance(outcome, HTTPError):
                self.error_streams.append(outcome.fp)
            raise outcome
        outcome.clock = self.clock
        self.responses.append(outcome)
        return outcome

    def query(self, **options):
        result = client.query_arxiv(MSSV, ARXIV_ID, **options)
        self.assertEqual(result["mssv"], MSSV)
        self.assertEqual(result["arxiv_id"], ARXIV_ID)
        self.assertFalse(client._request_lock.locked())
        return result

    def test_success_request_parameters_and_mapping(self):
        self.outcomes = [FakeResponse()]
        result = self.query(timeout=12)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(urlsplit(request.full_url).scheme, "https")
        self.assertEqual(urlsplit(request.full_url).netloc, "export.arxiv.org")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query),
                         {"id_list": [ARXIV_ID], "start": ["0"], "max_results": ["1"]})
        self.assertEqual(request.get_header("User-agent"), client.USER_AGENT)
        self.assertEqual(request.get_header("Accept"), "application/atom+xml")
        self.assertEqual(request.get_header("Connection"), "close")
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 12})
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["version_ids"],
                         [f"{ARXIV_ID}v1", f"{ARXIV_ID}v2", f"{ARXIV_ID}v3"])
        self.assertTrue(self.responses[0].closed)
        self.sleep.assert_not_called()

    def test_integration_with_assigned_id_pair(self):
        pair = next(iter_assigned_ids())
        self.outcomes = [FakeResponse(body=paper_xml(pair[1]))]
        result = client.query_arxiv(*pair)
        self.assertEqual((result["mssv"], result["arxiv_id"]), pair)
        self.assertEqual(result["status"], "found")

    def test_mssv_leading_zeros_and_custom_user_agent(self):
        self.outcomes = [FakeResponse()]
        result = client.query_arxiv("00120010", ARXIV_ID, user_agent="Group10-Test/1.0")
        self.assertEqual(result["mssv"], "00120010")
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), "Group10-Test/1.0")

    def test_missing_metadata_and_unknown_version_are_preserved(self):
        xml = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
               f'<id>http://arxiv.org/abs/{ARXIV_ID}</id></entry></feed>').encode()
        self.outcomes = [FakeResponse(body=xml)]
        result = self.query()
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["version_status"], "needs_verification")
        self.assertEqual(result["version_ids"], [])
        self.assertIn("title", result["missing_fields"])
        self.assertEqual(result["attempts"], 1)

    def test_not_found_requires_valid_success_response(self):
        xml = (b'<feed xmlns="http://www.w3.org/2005/Atom" '
               b'xmlns:o="http://a9.com/-/spec/opensearch/1.1/">'
               b'<o:totalResults>0</o:totalResults></feed>')
        self.outcomes = [FakeResponse(body=xml)]
        result = self.query()
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["attempts"], 1)

    def test_api_error_entry_is_not_retried(self):
        xml = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
               b'<id>http://arxiv.org/api/errors#incorrect_id_format</id>'
               b'<title>Error</title><summary>Invalid ID</summary></entry></feed>')
        self.outcomes = [FakeResponse(body=xml)]
        result = self.query()
        self.assertEqual(result["status"], "api_error")
        self.assertIn("Invalid ID", result["error"])
        self.assertEqual(result["attempts"], 1)

    def test_malformed_xml_is_not_retried(self):
        self.outcomes = [FakeResponse(body=b"<broken")]
        result = self.query()
        self.assertEqual(result["status"], "invalid_xml")
        self.assertEqual(result["attempts"], 1)

    def test_wrong_paper_id_is_not_retried(self):
        self.outcomes = [FakeResponse(body=paper_xml("2502.13234"))]
        result = self.query()
        self.assertEqual(result["status"], "invalid_response")
        self.assertEqual(result["attempts"], 1)

    def test_wrong_or_missing_content_type(self):
        for content_type in ("text/html", "application/json", None):
            with self.subTest(content_type=content_type):
                self.outcomes = [FakeResponse(content_type=content_type)]
                result = self.query()
                self.assertEqual(result["status"], "invalid_response")
                self.assertIn("Content-Type", result["error"])
                self.assertEqual(result["attempts"], 1)

    def test_xml_content_types_with_charset(self):
        for content_type in ("application/xml; charset=UTF-8", "text/xml", "Application/Atom+XML"):
            with self.subTest(content_type=content_type):
                self.outcomes = [FakeResponse(content_type=content_type)]
                self.assertEqual(self.query()["status"], "found")

    def test_other_2xx_statuses_are_not_treated_as_complete_xml(self):
        for status in (204, 206):
            with self.subTest(status=status):
                self.outcomes = [FakeResponse(status=status)]
                result = self.query()
                self.assertEqual(result["status"], "http_error")
                self.assertEqual(result["http_status"], status)
                self.assertEqual(result["attempts"], 1)

    def test_client_errors_are_not_retried_or_not_found(self):
        for code in (400, 401, 403, 404, 408, 422):
            with self.subTest(code=code):
                self.outcomes = [http_error(code)]
                result = self.query()
                self.assertEqual(result["status"], "http_error")
                self.assertEqual(result["http_status"], code)
                self.assertEqual(result["attempts"], 1)
                self.assertTrue(self.error_streams[-1].closed)

    def test_redirects_do_not_create_unpaced_requests(self):
        handlers = [handler for handler in self.original_opener.handlers
                    if isinstance(handler, HTTPRedirectHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertIsNone(handlers[0].redirect_request(
            Request(client.API_URL), None, 302, "Moved", headers(), client.API_URL
        ))
        self.outcomes = [http_error(302)]
        result = self.query()
        self.assertEqual(result["status"], "http_error")
        self.assertEqual(result["http_status"], 302)
        self.assertEqual(result["attempts"], 1)

    def test_shared_pacing_between_separate_calls(self):
        self.outcomes = [FakeResponse(), FakeResponse()]
        self.query()
        self.query()
        self.assertEqual(self.starts, [0.0, 3.0])
        self.sleep.assert_called_once_with(3.0)

    def test_connection_closes_before_cooldown_and_next_request(self):
        self.outcomes = [FakeResponse(duration=2), FakeResponse()]
        self.query()
        self.query()
        self.assertEqual(self.starts, [0.0, 5.0])
        self.assertTrue(all(response.closed for response in self.responses))

    def test_early_sleep_return_rechecks_deadline(self):
        self.outcomes = [FakeResponse(), FakeResponse()]
        self.query()
        waits = []

        def early_sleep(seconds):
            waits.append(seconds)
            self.clock.sleep(1.0 if len(waits) == 1 else seconds)

        self.sleep.side_effect = early_sleep
        self.query()
        self.assertEqual(waits, [3.0, 2.0])
        self.assertEqual(self.starts, [0.0, 3.0])

    def test_429_honors_retry_after_seconds(self):
        self.outcomes = [http_error(429, "120"), FakeResponse()]
        result = self.query()
        self.assertEqual(self.starts, [0.0, 120.0])
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["attempts"], 2)

    def test_503_honors_retry_after_http_date(self):
        date = formatdate(self.clock.wall_time() + 12, usegmt=True)
        self.outcomes = [http_error(503, date), FakeResponse()]
        result = self.query()
        self.assertEqual(self.starts, [0.0, 12.0])
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["attempts"], 2)

    def test_invalid_short_or_past_retry_after_uses_backoff(self):
        for value in ("bad", "-1", "1.5", "0", "1",
                      formatdate(self.clock.wall_time() - 100, usegmt=True)):
            with self.subTest(retry_after=value):
                self.outcomes = [http_error(503, value), FakeResponse()]
                result = self.query()
                self.assertEqual(self.starts[-1] - self.starts[-2], 3.0)
                self.assertEqual(result["status"], "found")

    def test_5xx_retry_limit_and_backoff(self):
        for code in (500, 501, 502, 503, 504, 599):
            with self.subTest(code=code):
                self.outcomes = [http_error(code) for _ in range(3)]
                result = self.query()
                self.assertEqual(result["status"], "server_error")
                self.assertEqual(result["http_status"], code)
                self.assertEqual(result["attempts"], 3)
                self.assertEqual(self.starts[-2] - self.starts[-3], 3.0)
                self.assertEqual(self.starts[-1] - self.starts[-2], 6.0)

    def test_backoff_growth_is_capped(self):
        self.outcomes = [http_error(503) for _ in range(7)] + [FakeResponse()]
        result = self.query(max_attempts=8)
        gaps = [later - earlier for earlier, later in zip(self.starts, self.starts[1:])]
        self.assertEqual(gaps, [3, 6, 12, 24, 48, 60, 60])
        self.assertEqual(result["attempts"], 8)

    def test_jitter_is_added_without_extra_retry(self):
        self.jitter.return_value = 0.5
        self.outcomes = [http_error(503), http_error(503), FakeResponse()]
        result = self.query()
        self.assertEqual(self.starts, [0.0, 3.5, 10.0])
        self.assertEqual(self.jitter.call_count, 2)
        self.assertEqual(result["attempts"], 3)

    def test_exhausted_429_keeps_retry_after_for_next_call(self):
        self.outcomes = [http_error(429, "15"), FakeResponse()]
        failed = self.query(max_attempts=1)
        self.assertEqual(failed["status"], "rate_limited")
        self.assertEqual(failed["attempts"], 1)
        self.sleep.assert_not_called()
        self.assertEqual(self.query()["status"], "found")
        self.assertEqual(self.starts, [0.0, 15.0])

    def test_timeout_dns_and_connection_errors_are_distinct(self):
        cases = [
            (URLError(TimeoutError("timed out")), "timeout"),
            (TimeoutError("timed out"), "timeout"),
            (URLError(socket.gaierror(-2, "DNS failed")), "dns_error"),
            (socket.gaierror(-2, "DNS failed"), "dns_error"),
            (URLError(ConnectionRefusedError("refused")), "connection_error"),
            (ConnectionResetError("reset"), "connection_error"),
        ]
        for error, status in cases:
            with self.subTest(status=status, error=type(error).__name__):
                self.outcomes = [error, error]
                result = self.query(max_attempts=2)
                self.assertEqual(result["status"], status)
                self.assertIsNone(result["http_status"])
                self.assertEqual(result["attempts"], 2)
                self.assertEqual(self.starts[-1] - self.starts[-2], 3.0)

    def test_read_timeout_closes_response_before_retry(self):
        self.outcomes = [FakeResponse(read_error=TimeoutError("read timeout")), FakeResponse()]
        result = self.query()
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(self.responses[0].closed)
        self.assertEqual(self.starts, [0.0, 3.0])

    def test_partial_read_is_connection_error(self):
        self.outcomes = [FakeResponse(read_error=IncompleteRead(b"partial", 100))]
        result = self.query(max_attempts=1)
        self.assertEqual(result["status"], "connection_error")
        self.assertEqual(result["http_status"], 200)
        self.assertTrue(self.responses[0].closed)

    def test_certificate_error_is_not_retried(self):
        self.outcomes = [URLError(ssl.SSLCertVerificationError(1, "certificate failed"))]
        result = self.query()
        self.assertEqual(result["status"], "tls_error")
        self.assertEqual(result["attempts"], 1)

    def test_timeout_cooldown_applies_to_next_query(self):
        self.outcomes = [TimeoutError("timeout"), FakeResponse()]
        self.assertEqual(self.query(max_attempts=1)["status"], "timeout")
        self.query()
        self.assertEqual(self.starts, [0.0, 3.0])

    def test_invalid_arguments_never_send_http(self):
        for mssv, arxiv_id in ((24120232, ARXIV_ID), ("", ARXIV_ID),
                                (MSSV, ARXIV_ID + "v1"), (MSSV, "bad")):
            with self.subTest(mssv=mssv, arxiv_id=arxiv_id), self.assertRaises(ValueError):
                client.query_arxiv(mssv, arxiv_id)
        cases = {
            "timeout": (0, -1, True, "30", float("nan"), float("inf")),
            "max_attempts": (0, -1, True, 1.5, "3"),
            "user_agent": (None, "", " ", "Bad\rAgent", "Bad\nAgent", "Tiếng Việt"),
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.query(**{field: value})
        self.opener.open.assert_not_called()
        self.sleep.assert_not_called()

    def test_unexpected_programming_errors_are_not_hidden(self):
        self.outcomes = [RuntimeError("unexpected bug")]
        with self.assertRaisesRegex(RuntimeError, "unexpected bug"):
            self.query()
        self.assertFalse(client._request_lock.locked())


if __name__ == "__main__":
    unittest.main()
