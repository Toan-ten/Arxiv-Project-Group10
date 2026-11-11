"""Semantic Scholar metadata/references client (Issues #5/#6), Python stdlib only.

No requests or environment key reads on import. No Storage or Pipeline writes.
All S2 consumers share DEFAULT_LIMITER; reference snapshots stay in memory.
"""
import copy
from collections import deque
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import json
import math
import random
from threading import Lock
import time
from datetime import date, datetime, timezone
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import ssl
import sys

from src.arxiv_discovery import ID_PATTERN
from src.local_config import get_semantic_scholar_api_key
from src import http_metrics


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


class _WaitBudgetExceeded(Exception):
    pass


class _WaitBudget:
    def __init__(self, seconds):
        _positive(seconds, "max_retry_wait")
        self.remaining = float(seconds)


def _retry_policy(max_retry_wait, backoff_base, backoff_cap):
    for value, name in ((max_retry_wait, "max_retry_wait"),
                        (backoff_base, "backoff_base"), (backoff_cap, "backoff_cap")):
        _positive(value, name)
    if backoff_base > backoff_cap:
        raise ValueError("backoff_base must not exceed backoff_cap")


class RateLimiter:
    """One lock/connection and a shared cooldown for all S2 consumers in a process.

    Interval is measured from response completion, conservatively. request_slot()
    yields postpone(seconds), called while holding the slot to extend cooldown.
    Share the same instance with future references code; do not make one per ID.
    Enforces min_interval pacing AND rolling window (100 req / 300s by default).
    """
    def __init__(
        self,
        min_interval=1.0,
        window_seconds=300.0,
        max_requests_per_window=100,
        monotonic_fn=None,
        sleep_fn=None,
    ):
        _positive(min_interval, "min_interval")
        _positive(window_seconds, "window_seconds")
        _positive(max_requests_per_window, "max_requests_per_window")
        self._min_interval = float(min_interval)
        self.window_seconds = float(window_seconds)
        self.max_requests_per_window = int(max_requests_per_window)
        self._monotonic = monotonic_fn or time.monotonic
        self._sleep = sleep_fn or time.sleep
        self._lock = Lock()
        self._next_request_at = 0.0
        self._cooldown_until = 0.0
        self._cooldown_status = None
        self._request_timestamps = deque()

    @property
    def min_interval(self):
        return self._min_interval

    def wait(self) -> float:
        with self._lock:
            total_slept = 0.0
            while True:
                now = self._monotonic()
                while self._request_timestamps and now - self._request_timestamps[0] >= self.window_seconds:
                    self._request_timestamps.popleft()

                sleep_needed = 0.0
                if now < self._next_request_at:
                    sleep_needed = max(sleep_needed, self._next_request_at - now)

                if len(self._request_timestamps) >= self.max_requests_per_window:
                    earliest = self._request_timestamps[0]
                    sleep_needed = max(sleep_needed, (earliest + self.window_seconds) - now)

                if sleep_needed <= 0.0:
                    break

                self._sleep(sleep_needed)
                total_slept += sleep_needed

            scheduled = self._monotonic()
            self._request_timestamps.append(scheduled)
            self._next_request_at = scheduled + self._min_interval
            return total_slept

    @contextmanager
    def request_slot(self, *, budget=None):
        with self._lock:
            while True:
                now = time.monotonic()
                while self._request_timestamps and now - self._request_timestamps[0] >= self.window_seconds:
                    self._request_timestamps.popleft()

                delay = 0.0
                if (int_delay := self._next_request_at - now) > 0:
                    delay = max(delay, int_delay)
                if len(self._request_timestamps) >= self.max_requests_per_window:
                    earliest = self._request_timestamps[0]
                    win_delay = (earliest + self.window_seconds) - now
                    delay = max(delay, win_delay)

                if delay <= 0:
                    break

                if budget is not None and delay > budget.remaining:
                    raise _WaitBudgetExceeded()
                started = time.monotonic()
                try:
                    time.sleep(delay)
                finally:
                    elapsed = max(0.0, time.monotonic() - started)
                    http_metrics.waited("semantic_scholar", elapsed)
                    if budget is not None:
                        budget.remaining -= elapsed

            if budget is not None and budget.remaining < 0:
                raise _WaitBudgetExceeded()

            self._request_timestamps.append(time.monotonic())

            def postpone(seconds, *, http_status=None):
                if (type(seconds) not in (int, float)
                        or not math.isfinite(seconds) or seconds < 0):
                    raise ValueError("Cooldown must be finite and nonnegative")
                deadline = time.monotonic() + seconds
                self._next_request_at = max(self._next_request_at, deadline)
                if seconds > 0 and deadline >= self._cooldown_until:
                    self._cooldown_until = deadline
                    self._cooldown_status = http_status

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
        # An overflowing positive integer is a very long valid delay, not
        # an absent header. Keep it beyond any practical wait budget.
        if not math.isfinite(seconds) and value.isascii() and value.isdigit():
            return sys.float_info.max
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _key_from_environment():
    # Keep the existing integration boundary; resolve the optional root .env lazily.
    return get_semantic_scholar_api_key()

def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("Nonfinite JSON")


def _json_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite JSON")
    return number


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
            body.decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_nonfinite, parse_float=_json_float,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise _InvalidResponse("invalid_json") from None


def _validate_batch(value, identifiers, require_arxiv_identity=True):
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
        if external is None and require_arxiv_identity:
            raise _InvalidResponse("arxiv_identity_unverified")
        if external is not None:
            match = ID_PATTERN.fullmatch(external) if isinstance(external, str) else None
            if match is None or match.group("base") != requested:
                raise _InvalidResponse("arxiv_identity_mismatch")
    return value


def _contains_secret(value, key):
    if isinstance(value, str):
        return key in value
    if isinstance(value, dict):
        return any(key in name or _contains_secret(item, key) for name, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item, key) for item in value)
    return False


def _request_json(url, *, method, body, validate, timeout, max_attempts, limiter, key,
                  max_response_bytes, max_retry_wait=60.0, backoff_base=1.0, backoff_cap=60.0):
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key is not None:
        headers["x-api-key"] = key
    budget = _WaitBudget(max_retry_wait)
    previous = None
    for attempt in range(1, max_attempts + 1):
        http_status, delay, retry, payload, error = None, 0.0, False, None, None
        try:
            slot = limiter.request_slot(budget=budget)
            postpone = slot.__enter__()
        except _WaitBudgetExceeded:
            if previous is not None:
                previous["error"] = "retry_wait_budget_exceeded"
                return previous
            observed = limiter._cooldown_status if limiter._cooldown_until > time.monotonic() else None
            return dict(status="rate_limited" if observed == 429 else "http_error",
                        http_status=observed, attempts=0, payload=None,
                        error="cooldown_wait_budget_exceeded")
        try:
            try:
                request = Request(url, data=body, headers=headers, method=method)
                with http_metrics.request("semantic_scholar", attempt) as observe:
                    with _opener.open(request, timeout=timeout) as response:
                        http_status = response.status
                        observe(http_status)
                        if type(http_status) is not int or not 100 <= http_status <= 599:
                            http_status = None
                            raise _InvalidResponse("invalid_http_status")
                        if http_status != 200:
                            # This diagnostic exception owns a buffer even with fp=None.
                            # Close it before response.__exit__ can replace the error.
                            with HTTPError(url, http_status, "S2 HTTP failure", response.headers, None) as failure:
                                raise failure
                        delay = _retry_after(response.headers.get("Retry-After"))
                        value = _read_json(response, max_response_bytes)
                        if key is not None and _contains_secret(value, key):
                            raise _InvalidResponse("response_contains_secret")
                        payload = validate(value)
                status = "found"  # Validated payload; caller assigns its stage/item status.
            except HTTPError as exc:
                http_status = exc.code
                try:
                    delay = _retry_after(exc.headers.get("Retry-After")) if exc.headers else 0.0
                    status = "rate_limited" if http_status == 429 else "http_error"
                    error = "http_413_request_too_large" if http_status == 413 else "http_" + str(http_status)
                    retry = http_status == 429 or 500 <= http_status <= 599
                finally:
                    try:
                        exc.close()
                    except Exception:
                        if http_status == 429:
                            # A cleanup failure must never hide the stop signal.
                            status, error, retry = "rate_limited", "http_429", False
                        else:
                            status, error, retry = "http_error", "transport_error", False
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
            if http_status == 429 and status != "rate_limited":
                status, error, retry = "rate_limited", "http_429", False
            if status != "found":
                payload = None
            if retry and attempt < max_attempts:
                delay = max(delay, min(backoff_base * 2.0 ** min(attempt - 1, 30) + random.uniform(0, 1), backoff_cap))
            postpone(delay, http_status=http_status)
        finally:
            slot.__exit__(None, None, None)
        previous = dict(status=status, http_status=http_status, attempts=attempt, payload=payload, error=error)
        if not retry or attempt == max_attempts:
            return dict(status=status, http_status=http_status, attempts=attempt, payload=payload, error=error)


def _request_batch(identifiers, *, timeout, max_attempts, limiter, key, max_response_bytes,
                    require_arxiv_identity=True, max_retry_wait=60.0,
                    backoff_base=1.0, backoff_cap=60.0):
    body = json.dumps({"ids": ["ARXIV:" + identifier for identifier in identifiers]}).encode("utf-8")
    url = BATCH_URL + "?" + urlencode({"fields": ",".join(METADATA_FIELDS)})
    result = _request_json(
        url, method="POST", body=body, validate=lambda value: _validate_batch(value, identifiers, require_arxiv_identity),
        timeout=timeout, max_attempts=max_attempts, limiter=limiter, key=key,
        max_response_bytes=max_response_bytes, max_retry_wait=max_retry_wait,
        backoff_base=backoff_base, backoff_cap=backoff_cap,
    )
    result["papers"] = result.pop("payload")
    return result


def fetch_metadata(arxiv_ids, *, batch_size=100, timeout=30.0, max_attempts=3,
                   limiter=None, max_response_bytes=MAX_RESPONSE_BYTES,
                   require_arxiv_identity=True, max_retry_wait=60.0,
                   backoff_base=1.0, backoff_cap=60.0):
    """Return one result per input position, preserving IDs/order and duplicates.

    Rate-limit exhaustion stops the call. Remaining, unsent inputs are marked
    rate_limited with http_status=None, attempts=0, and a not_attempted error.
    Size/structure failures never auto-split/retry a chunk. No reference requests.
    Identity is checked by default; False explicitly opts into unverified positional mapping.
    """
    _positive(timeout, "timeout")
    _retry_policy(max_retry_wait, backoff_base, backoff_cap)
    if type(require_arxiv_identity) is not bool:
        raise ValueError("require_arxiv_identity must be a boolean")
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
            require_arxiv_identity=require_arxiv_identity,
            max_retry_wait=max_retry_wait, backoff_base=backoff_base, backoff_cap=backoff_cap,
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
        if response["status"] == "rate_limited" or response["http_status"] == 403:
            results.extend(dict(
                arxiv_id=identifier, status=response["status"], http_status=None,
                attempts=0, paper=None, error=("not_attempted_after_rate_limit"
                    if response["status"] == "rate_limited" else "not_attempted_after_http_403"),
            ) for identifier in identifiers[start + len(chunk):])
            break
    return results

REFERENCE_FIELDS = (
    "paperId", "corpusId", "title", "authors", "year", "venue",
    "publicationVenue", "publicationDate", "externalIds", "url",
    "contexts", "intents", "isInfluential",
)
_REFERENCE_ITEM_FIELDS = (
    "arxiv_id", "title", "authors", "submission_date", "publication_date",
    "year", "venue", "s2_paper_id",
)


def _normalize_reference(raw):
    if not isinstance(raw, dict):
        raise _InvalidResponse("invalid_reference_item")
    paper = raw.get("citedPaper")
    if paper is not None and not isinstance(paper, dict):
        raise _InvalidResponse("invalid_cited_paper")
    paper = paper or {}
    for name in ("paperId", "title", "venue", "url", "publicationDate"):
        if paper.get(name) is not None and not isinstance(paper[name], str):
            raise _InvalidResponse("invalid_reference_field")
    # corpusId is not part of the normalized M1 reference record. Preserve it
    # verbatim in raw, but do not reject an otherwise usable page if the API
    # changes the type of this ancillary field.
    if paper.get("year") is not None and (type(paper["year"]) is not int or paper["year"] < 0):
        raise _InvalidResponse("invalid_reference_field")
    for name in ("externalIds", "publicationVenue"):
        if paper.get(name) is not None and not isinstance(paper[name], dict):
            raise _InvalidResponse("invalid_reference_field")
    for name in ("contexts", "intents"):
        if raw.get(name) is not None and (
            not isinstance(raw[name], list) or any(not isinstance(v, str) for v in raw[name])
        ):
            raise _InvalidResponse("invalid_reference_relation")
    if raw.get("isInfluential") is not None and type(raw["isInfluential"]) is not bool:
        raise _InvalidResponse("invalid_reference_relation")
    authors = paper.get("authors")
    if authors is not None and (
        not isinstance(authors, list) or any(not isinstance(a, dict) for a in authors)
    ):
        raise _InvalidResponse("invalid_reference_authors")
    names, incomplete_authors = [], False
    for author in authors or []:
        name = author.get("name")
        if name is not None and not isinstance(name, str):
            raise _InvalidResponse("invalid_reference_authors")
        if name and name.strip():
            names.append(name)
        else:
            incomplete_authors = True
    normalized_authors = None if authors is None or incomplete_authors and not names else names
    publication_date = paper.get("publicationDate")
    if publication_date is not None:
        try:
            valid_date = date.fromisoformat(publication_date).isoformat() == publication_date
        except ValueError:
            valid_date = False
        if not valid_date:
            raise _InvalidResponse("invalid_publication_date")
    external = (paper.get("externalIds") or {}).get("ArXiv")
    if external is not None and not isinstance(external, str):
        raise _InvalidResponse("invalid_reference_arxiv_id")
    identifier = external if external and ID_PATTERN.fullmatch(external) else None
    def text(name):
        value = paper.get(name)
        return value if value and value.strip() else None
    result = dict(
        arxiv_id=identifier, title=text("title"), authors=normalized_authors,
        submission_date=None, publication_date=publication_date, year=paper.get("year"),
        venue=text("venue"), s2_paper_id=text("paperId"),
    )
    result["missing_fields"] = [
        name for name in _REFERENCE_ITEM_FIELDS
        if result[name] is None or result[name] == []
        or name == "authors" and incomplete_authors
    ]
    result["raw"] = copy.deepcopy(raw)
    return result


def _reference_page(value, offset, limit):
    # Reject a bad page atomically; never drop its bad item and shift the rest.
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        raise _InvalidResponse("invalid_references_page")
    if type(value.get("offset")) is not int or value["offset"] != offset:
        raise _InvalidResponse("invalid_page_offset")
    if len(value["data"]) > limit:
        raise _InvalidResponse("page_exceeds_limit")
    return value, [_normalize_reference(item) for item in value["data"]]


def _fetch_references(arxiv_id, *, fields=None, limit=100, max_pages=1000,
                     timeout=30.0, max_attempts=3, limiter=None,
                     max_response_bytes=MAX_RESPONSE_BYTES, max_retry_wait=60.0,
                     backoff_base=1.0, backoff_cap=60.0):
    """Fetch a fresh v2 references snapshot in memory, starting at offset zero.

    No corpus/checkpoint writes, no Pipeline integration and no deduplication.
    'error' is an optional v2-compatible diagnostic extension (fixed codes only).
    A complete snapshot proves API pagination coverage, not LaTeX coverage.
    """
    _positive(timeout, "timeout")
    _retry_policy(max_retry_wait, backoff_base, backoff_cap)
    match = ID_PATTERN.fullmatch(arxiv_id) if isinstance(arxiv_id, str) else None
    if match is None or match.group("version") is not None:
        raise ValueError("Expected a base arXiv ID without prefixes or version suffixes")
    for value, name, maximum in ((limit, "limit", 1000), (max_pages, "max_pages", None),
                                 (max_attempts, "max_attempts", None),
                                 (max_response_bytes, "max_response_bytes", MAX_RESPONSE_BYTES)):
        if type(value) is not int or value < 1 or maximum is not None and value > maximum:
            raise ValueError("Invalid " + name)
    if fields is None:
        fields = REFERENCE_FIELDS
    elif isinstance(fields, str):
        fields = tuple(part.strip() for part in fields.split(","))
    if (not isinstance(fields, (tuple, list)) or not fields
            or any(not isinstance(f, str) or f not in REFERENCE_FIELDS for f in fields)
            or len(set(fields)) != len(fields)):
        raise ValueError("Unsupported or duplicate reference fields")
    limiter = DEFAULT_LIMITER if limiter is None else limiter
    if not isinstance(limiter, RateLimiter):
        raise ValueError("limiter must be a shared RateLimiter")
    key = _key_from_environment()
    snapshot = dict(
        contract_version=2, arxiv_id=arxiv_id, status="failed",
        provenance=dict(provider="semantic_scholar", query_identifier="ARXIV:" + arxiv_id,
                        s2_paper_id=None, retrieved_at=None),
        coverage=dict(complete=False, continuation=None, reported_total=None, retrieved_count=0),
        items=[], error=None,
    )
    coverage = snapshot["coverage"]
    offset, seen = 0, set()

    def fail(kind, message, *, http_status, attempts, failed_offset):
        snapshot["status"] = "partial" if snapshot["items"] else "failed"
        snapshot["error"] = dict(type=kind, message=message, http_status=http_status,
                                 attempts=attempts, offset=failed_offset)
        return snapshot

    for page_index in range(max_pages):
        seen.add(offset)
        url = (BATCH_URL.removesuffix("/batch") + "/"
               + quote("ARXIV:" + arxiv_id, safe=":") + "/references?"
               + urlencode(dict(fields=",".join(fields), offset=offset, limit=limit)))
        response = _request_json(
            url, method="GET", body=None,
            validate=lambda value: _reference_page(value, offset, limit),
            timeout=timeout, max_attempts=max_attempts, limiter=limiter, key=key,
            max_response_bytes=max_response_bytes, max_retry_wait=max_retry_wait,
            backoff_base=backoff_base, backoff_cap=backoff_cap,
        )
        snapshot["provenance"]["retrieved_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if response["payload"] is None:
            coverage["continuation"] = {"offset": offset}
            result = fail(response["status"], response["error"],
                          http_status=response["http_status"], attempts=response["attempts"],
                          failed_offset=offset)
            if response["http_status"] == 404 and page_index == 0:
                result["status"] = "not_found"
                coverage["continuation"] = None
            return result
        page, items = response["payload"]
        snapshot["items"].extend(items)
        coverage["retrieved_count"] = len(snapshot["items"])
        coverage["continuation"] = None
        error = None
        total = page.get("total")
        previous_total = coverage["reported_total"]
        if "total" in page and (
            type(total) is not int or total < coverage["retrieved_count"]
            or previous_total is not None and total != previous_total
        ):
            error = "invalid_reported_total"
        elif total is not None:
            coverage["reported_total"] = total
        if (coverage["reported_total"] is not None
                and coverage["reported_total"] < coverage["retrieved_count"]):
            coverage["reported_total"] = None
            error = "invalid_reported_total"
        next_offset = page.get("next")
        if next_offset is not None:
            if type(next_offset) is not int:
                error = error or "invalid_next_type"
            elif next_offset in seen or next_offset <= offset:
                error = error or "pagination_not_advancing"
            elif not items or next_offset != offset + len(items):
                error = error or "pagination_gap_or_overlap"
            else:
                coverage["continuation"] = {"offset": next_offset}
                if (coverage["reported_total"] is not None
                        and next_offset >= coverage["reported_total"]):
                    error = error or "next_conflicts_with_total"
        elif (coverage["reported_total"] is not None
              and coverage["reported_total"] != coverage["retrieved_count"]):
            error = error or "terminal_total_mismatch"
        if error is not None:
            coverage["continuation"] = None
            return fail("invalid_response", error, http_status=200,
                        attempts=response["attempts"], failed_offset=offset)
        if next_offset is None:
            snapshot["status"] = "complete"
            coverage["complete"] = True
            return snapshot
        if page_index + 1 == max_pages:
            return fail("pagination_limit", "max_pages_reached", http_status=None,
                        attempts=0, failed_offset=next_offset)
        offset = next_offset

def _validated_resume(arxiv_id, previous, key):
    """Validate a client snapshot, not the authenticity of an on-disk checkpoint."""
    def require(condition, code):
        if not condition:
            raise ValueError("Invalid resume snapshot: " + code)
    require(isinstance(previous, dict), "expected_object")
    try:
        old = json.loads(json.dumps(previous, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Invalid resume snapshot: invalid_json") from None
    require(type(old.get("contract_version")) is int and old["contract_version"] == 2,
            "contract_version")
    require(old.get("arxiv_id") == arxiv_id, "publication_mismatch")
    require(isinstance(old.get("status"), str)
            and old["status"] in {"partial", "failed", "interrupted"}, "status")
    provenance = old.get("provenance")
    require(isinstance(provenance, dict), "provenance")
    require(provenance.get("provider") == "semantic_scholar"
            and provenance.get("query_identifier") == "ARXIV:" + arxiv_id,
            "query_identity")
    require("s2_paper_id" in provenance and provenance["s2_paper_id"] is None,
            "unsupported_parent_identity")
    stamp = provenance.get("retrieved_at")
    require(isinstance(stamp, str), "retrieved_at")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid resume snapshot: retrieved_at") from None
    require(parsed.tzinfo is not None, "retrieved_at_timezone")
    coverage, items = old.get("coverage"), old.get("items")
    require(isinstance(coverage, dict) and isinstance(items, list), "coverage_or_items")
    require(type(coverage.get("complete")) is bool and coverage["complete"] is False,
            "incomplete_flag")
    require(type(coverage.get("retrieved_count")) is int
            and coverage["retrieved_count"] == len(items), "retrieved_count")
    continuation = coverage.get("continuation")
    require(isinstance(continuation, dict) and set(continuation) == {"offset"}
            and type(continuation["offset"]) is int
            and continuation["offset"] == len(items), "continuation")
    require("reported_total" in coverage, "reported_total")
    total = coverage["reported_total"]
    require(total is None or type(total) is int and total >= len(items), "reported_total")
    for item in items:
        require(isinstance(item, dict) and isinstance(item.get("raw"), dict), "item_raw")
        try:
            normalized = _normalize_reference(item["raw"])
        except _InvalidResponse:
            raise ValueError("Invalid resume snapshot: invalid_item_raw") from None
        require(item == normalized, "normalized_item_mismatch")
    require(key is None or not _contains_secret(old, key), "contains_secret")
    return old


def _reconcile_resume(previous, current):
    """Use a fresh traversal only if it preserves the saved prefix verbatim."""
    old_items, new_items = previous["items"], current["items"]
    count = min(len(old_items), len(new_items))
    conflict = None
    if old_items[:count] != new_items[:count]:
        conflict = "resume_prefix_mismatch"
    elif current["status"] == "not_found" and old_items:
        conflict = "resume_source_not_found"
    elif current["status"] == "complete" and len(new_items) < len(old_items):
        conflict = "resume_prefix_truncated"
    if conflict is None and len(new_items) >= len(old_items):
        return current
    # Do not mutate the caller's snapshot or stitch data from two traversals.
    preserved = copy.deepcopy(previous)
    preserved["status"] = "partial" if old_items else "failed"
    preserved["coverage"]["complete"] = False
    if conflict is not None:
        preserved["coverage"]["continuation"] = None
    if current["error"] is not None:
        # In particular, never replace a final 429 with a resume diagnostic.
        preserved["error"] = copy.deepcopy(current["error"])
        if conflict is not None:
            preserved["error"]["resume_reason"] = conflict
    else:
        preserved["error"] = dict(
            type="invalid_response", message=conflict,
            http_status=None, attempts=None, offset=None,
        )
    preserved["error"]["observed_at"] = current["provenance"]["retrieved_at"]
    return preserved


def fetch_references(arxiv_id, *, fields=None, limit=100, max_pages=1000,
                     timeout=30.0, max_attempts=3, limiter=None,
                     max_response_bytes=MAX_RESPONSE_BYTES, resume_from=None,
                     max_retry_wait=60.0, backoff_base=1.0, backoff_cap=60.0):
    """Return a v2 snapshot; optional resume restarts and revalidates its prefix.

    This never skips straight to a saved offset or concatenates two snapshots.
    It costs a new traversal from zero; the API has no immutable snapshot token.
    Storage validation/persistence and inter-process locking remain separate.
    """
    previous = None
    if resume_from is not None:
        match = ID_PATTERN.fullmatch(arxiv_id) if isinstance(arxiv_id, str) else None
        if match is None or match.group("version") is not None:
            raise ValueError("Expected a base arXiv ID without prefixes or version suffixes")
        previous = _validated_resume(arxiv_id, resume_from, _key_from_environment())
    current = _fetch_references(
        arxiv_id, fields=fields, limit=limit, max_pages=max_pages, timeout=timeout,
        max_attempts=max_attempts, limiter=limiter, max_response_bytes=max_response_bytes,
        max_retry_wait=max_retry_wait, backoff_base=backoff_base, backoff_cap=backoff_cap,
    )
    return current if previous is None else _reconcile_resume(previous, current)
