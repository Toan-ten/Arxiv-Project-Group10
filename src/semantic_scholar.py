"""Semantic Scholar metadata batch client (Issue #5), Python stdlib only.

No requests or environment key reads on import. No Storage or Pipeline writes.
All default S2 consumers share DEFAULT_LIMITER; references are not implemented.
"""
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import json
import math
import os
import random
from threading import Lock
import time
from datetime import timezone
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import ssl

from src.arxiv_discovery import ID_PATTERN


BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
USER_AGENT = "Arxiv-Project-Group10/0.1 (+https://github.com/Toan-ten/Arxiv-Project-Group10)"
MAX_RESPONSE_BYTES = 10_000_000
METADATA_FIELDS = (
    "paperId", "corpusId", "externalIds", "title", "abstract", "authors", "year",
    "venue", "publicationVenue", "publicationTypes", "journal", "citationCount",
    "influentialCitationCount", "referenceCount", "fieldsOfStudy", "s2FieldsOfStudy",
    "openAccessPdf", "url",
)


def _positive(value, name):
    if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
        raise ValueError(name + " must be finite and positive")


class RateLimiter:
    """One lock/connection and a shared cooldown for all S2 consumers in a process.

    Interval is measured from response completion, conservatively. request_slot()
    yields postpone(seconds), called while holding the slot to extend cooldown.
    Share the same instance with future references code; do not make one per ID.
    """
    def __init__(self, min_interval=1.0):
        _positive(min_interval, "min_interval")
        self._min_interval = float(min_interval)
        self._lock = Lock()
        self._next_request_at = 0.0

    @property
    def min_interval(self):
        return self._min_interval

    @contextmanager
    def request_slot(self):
        with self._lock:
            while (delay := self._next_request_at - time.monotonic()) > 0:
                time.sleep(delay)

            def postpone(seconds):
                if (type(seconds) not in (int, float)
                        or not math.isfinite(seconds) or seconds < 0):
                    raise ValueError("Cooldown must be finite and nonnegative")
                self._next_request_at = max(self._next_request_at, time.monotonic() + seconds)

            try:
                yield postpone
            finally:
                self._next_request_at = max(
                    self._next_request_at, time.monotonic() + self.min_interval,
                )


DEFAULT_LIMITER = RateLimiter()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never send credentials to a redirect target or issue an unpaced GET.
        return None


_opener = build_opener(_NoRedirect())


class _InvalidResponse(ValueError):
    """A fixed diagnostic code; never the response body or credentials."""


def _retry_after(value):
    if not isinstance(value, str):
        return 0.0
    value = value.strip()
    try:
        if value.isascii() and value.isdigit():
            seconds = float(value)
        else:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = parsed.timestamp() - time.time()
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _key_from_environment():
    value = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    if not value:
        return None
    if "\r" in value or "\n" in value:
        raise ValueError("Invalid SEMANTIC_SCHOLAR_API_KEY header")
    value = value.strip()
    if not value:
        return None
    if not value.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("Invalid SEMANTIC_SCHOLAR_API_KEY header")
    return value


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("Nonfinite JSON")


def _read_json(response, limit):
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json" and not content_type.endswith("+json"):
        raise _InvalidResponse("unexpected_content_type")
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"", "identity"}:
        raise _InvalidResponse("unsupported_content_encoding")
    length = response.headers.get("Content-Length")
    if length is not None:
        if not isinstance(length, str) or not length.isascii() or not length.isdigit():
            raise _InvalidResponse("invalid_content_length")
        if int(length) > limit:
            raise _InvalidResponse("response_too_large")
    body = response.read(limit + 1)
    if not isinstance(body, bytes):
        raise _InvalidResponse("invalid_response_body")
    if len(body) > limit:
        raise _InvalidResponse("response_too_large")
    if length is not None and int(length) != len(body):
        raise _InvalidResponse("content_length_mismatch")
    try:
        return json.loads(
            body.decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_nonfinite,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise _InvalidResponse("invalid_json") from None


def _validate_batch(value, identifiers):
    if not isinstance(value, list) or len(value) != len(identifiers):
        raise _InvalidResponse("invalid_batch_length_or_structure")
    # Validate the entire chunk before assigning even its first record.
    for requested, paper in zip(identifiers, value):
        if paper is None:
            continue
        if (not isinstance(paper, dict) or not isinstance(paper.get("paperId"), str)
                or not paper["paperId"].strip()):
            raise _InvalidResponse("invalid_paper")
        for name in ("corpusId", "year", "citationCount", "influentialCitationCount", "referenceCount"):
            number = paper.get(name)
            if number is not None and (type(number) is not int or number < 0):
                raise _InvalidResponse("invalid_metadata_field")
        for name in ("title", "abstract", "venue", "url"):
            if paper.get(name) is not None and not isinstance(paper[name], str):
                raise _InvalidResponse("invalid_metadata_field")
        for name in ("publicationVenue", "journal", "openAccessPdf", "externalIds"):
            if paper.get(name) is not None and not isinstance(paper[name], dict):
                raise _InvalidResponse("invalid_metadata_field")
        for name in ("fieldsOfStudy", "publicationTypes"):
            if paper.get(name) is not None and (
                not isinstance(paper[name], list) or any(not isinstance(v, str) for v in paper[name])
            ):
                raise _InvalidResponse("invalid_metadata_field")
        for name in ("authors", "s2FieldsOfStudy"):
            if paper.get(name) is not None and (
                not isinstance(paper[name], list) or any(not isinstance(v, dict) for v in paper[name])
            ):
                raise _InvalidResponse("invalid_metadata_field")
        for author in paper.get("authors") or []:
            if any(author.get(name) is not None and not isinstance(author[name], str)
                   for name in ("authorId", "name")):
                raise _InvalidResponse("invalid_metadata_field")
        external = (paper.get("externalIds") or {}).get("ArXiv")
        if external is not None:
            match = ID_PATTERN.fullmatch(external) if isinstance(external, str) else None
            if match is None or match.group("base") != requested:
                raise _InvalidResponse("arxiv_identity_mismatch")
    return value


def _request_batch(identifiers, *, timeout, max_attempts, limiter, key, max_response_bytes):
    body = json.dumps({"ids": ["ARXIV:" + identifier for identifier in identifiers]}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Accept-Encoding": "identity", "User-Agent": USER_AGENT}
    if key is not None:
        headers["x-api-key"] = key
    url = BATCH_URL + "?" + urlencode({"fields": ",".join(METADATA_FIELDS)})
    for attempt in range(1, max_attempts + 1):
        http_status, delay, retry, papers, error = None, 0.0, False, None, None
        with limiter.request_slot() as postpone:
            try:
                request = Request(url, data=body, headers=headers, method="POST")
                with _opener.open(request, timeout=timeout) as response:
                    http_status = response.status
                    if type(http_status) is not int or not 100 <= http_status <= 599:
                        http_status = None
                        raise _InvalidResponse("invalid_http_status")
                    if http_status != 200:
                        raise HTTPError(url, http_status, "S2 HTTP failure", response.headers, None)
                    delay = _retry_after(response.headers.get("Retry-After"))
                    papers = _validate_batch(_read_json(response, max_response_bytes), identifiers)
                status = "found"  # Per-item found/not_found is assigned after whole-chunk validation.
            except HTTPError as exc:
                http_status = exc.code
                try:
                    delay = _retry_after(exc.headers.get("Retry-After")) if exc.headers else 0.0
                    status = "rate_limited" if http_status == 429 else "http_error"
                    error = "http_413_request_too_large" if http_status == 413 else "http_" + str(http_status)
                    retry = http_status == 429 or 500 <= http_status <= 599
                finally:
                    exc.close()
            except _InvalidResponse as exc:
                status, error = "invalid_response", str(exc)  # Only our fixed codes.
            except (URLError, OSError, HTTPException) as exc:
                reason = exc.reason if isinstance(exc, URLError) else exc
                status = "timeout" if isinstance(reason, TimeoutError) else "http_error"
                error = "timeout" if status == "timeout" else "network_error"
                retry = not isinstance(reason, ssl.SSLError)
            except Exception:
                # A transport/header implementation may include credentials in
                # exception text. Do not stringify or chain that exception.
                status, error, retry = "http_error", "transport_error", False
            if status != "found":
                papers = None
            if retry and attempt < max_attempts:
                delay = max(delay, min(2.0 ** min(attempt - 1, 6), 60.0) + random.uniform(0, 1))
            postpone(delay)
        if not retry or attempt == max_attempts:
            return dict(status=status, http_status=http_status, attempts=attempt, papers=papers, error=error)


def fetch_metadata(arxiv_ids, *, batch_size=100, timeout=30.0, max_attempts=3,
                   limiter=None, max_response_bytes=MAX_RESPONSE_BYTES):
    """Return one result per input position, preserving IDs/order and duplicates.

    Rate-limit exhaustion stops the call. Remaining, unsent inputs are marked
    rate_limited with http_status=None, attempts=0, and a not_attempted error.
    Size/structure failures never auto-split/retry a chunk. No reference requests.
    """
    _positive(timeout, "timeout")
    if type(batch_size) is not int or not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be an integer between 1 and 500")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
        raise ValueError("max_response_bytes must be between 1 and 10000000")
    if not isinstance(arxiv_ids, (list, tuple)):
        raise ValueError("arxiv_ids must be an explicit list/tuple")
    identifiers = list(arxiv_ids)
    for identifier in identifiers:
        match = ID_PATTERN.fullmatch(identifier) if isinstance(identifier, str) else None
        if match is None or match.group("version") is not None:
            raise ValueError("Expected base arXiv IDs without prefixes or version suffixes")
    limiter = DEFAULT_LIMITER if limiter is None else limiter
    if not isinstance(limiter, RateLimiter):
        raise ValueError("limiter must be a shared RateLimiter")
    if not identifiers:
        return []
    key = _key_from_environment()
    results = []
    for start in range(0, len(identifiers), batch_size):
        chunk = identifiers[start:start + batch_size]
        response = _request_batch(
            chunk, timeout=timeout, max_attempts=max_attempts, limiter=limiter,
            key=key, max_response_bytes=max_response_bytes,
        )
        for index, identifier in enumerate(chunk):
            paper = response["papers"][index] if response["papers"] is not None else None
            status = response["status"]
            if response["papers"] is not None:
                status = "not_found" if paper is None else "found"
            results.append(dict(
                arxiv_id=identifier, status=status, http_status=response["http_status"],
                attempts=response["attempts"], paper=paper, error=response["error"],
            ))
        if response["status"] == "rate_limited":
            results.extend(dict(
                arxiv_id=identifier, status="rate_limited", http_status=None,
                attempts=0, paper=None, error="not_attempted_after_rate_limit",
            ) for identifier in identifiers[start + len(chunk):])
            break
    return results
