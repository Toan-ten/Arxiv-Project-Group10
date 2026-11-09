import math
import random
import re
import socket
import ssl
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from src import http_metrics
from src.arxiv_discovery import ID_PATTERN, parse_arxiv_response


API_URL = "https://export.arxiv.org/api/query"
USER_AGENT = (
    "Arxiv-Project-Group10/0.1 "
    "(+https://github.com/Toan-ten/Arxiv-Project-Group10)"
)
MIN_INTERVAL = 3.0
RETRY_429_FALLBACK_SECONDS = 60.0
PHI = (1.0 + math.sqrt(5.0)) / 2.0
XML_CONTENT_TYPES = {"application/atom+xml", "application/xml", "text/xml"}


class _NoRedirect(HTTPRedirectHandler):
    # urllib redirects would send another request without waiting.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(_NoRedirect())
_request_lock = Lock()
_next_request_at = 0.0
_REQUEST_LOG = ContextVar("arxiv_request_log", default=None)
_RETRY_429 = ContextVar("arxiv_experimental_retry_429", default=None)
_BACKOFF_SCHEDULE = ContextVar("arxiv_backoff_schedule", default=None)


class _Retry429:
    """One explicit Production Run's budget; ordinary pacing is not charged."""

    def __init__(self, budget):
        if type(budget) not in (int, float) or not math.isfinite(budget) or budget < 0:
            raise ValueError("429 wait budget must be a finite nonnegative number")
        self.budget = float(budget)
        self.waited = 0.0
        self.exhausted = False

    @property
    def remaining(self):
        return max(0.0, self.budget - self.waited)

    def snapshot(self):
        return dict(mode="retry_once", wait_budget_seconds=self.budget,
                    waited_seconds=self.waited, remaining_seconds=self.remaining,
                    budget_exhausted=self.exhausted, fallback_seconds=RETRY_429_FALLBACK_SECONDS)


@contextmanager
def retry_429_policy(wait_budget_seconds=None):
    """Opt-in bounded 429 retry shared across cohorts in one Production Run.

    Requires max_attempts=1 and a durable request sink. Restart admission still
    checks stored deadlines; an interrupted Run never restarts itself.
    """
    policy = None if wait_budget_seconds is None else _Retry429(wait_budget_seconds)
    token = _RETRY_429.set(policy)
    try:
        yield policy
    finally:
        _RETRY_429.reset(token)


class _BackoffPolicy:
    """Configurable Golden-Ratio backoff schedule with optional cumulative wait budget."""

    def __init__(
        self,
        base_seconds: float = 8.0,
        cap_seconds: float = 60.0,
        wait_budget_seconds: float | None = None,
    ):
        if type(base_seconds) not in (int, float) or not math.isfinite(base_seconds) or base_seconds < MIN_INTERVAL:
            raise ValueError("base_seconds must be a finite number >= MIN_INTERVAL")
        if type(cap_seconds) not in (int, float) or not math.isfinite(cap_seconds) or cap_seconds < base_seconds:
            raise ValueError("cap_seconds must be a finite number >= base_seconds")
        if wait_budget_seconds is not None and (
            type(wait_budget_seconds) not in (int, float)
            or not math.isfinite(wait_budget_seconds)
            or wait_budget_seconds < 0
        ):
            raise ValueError("wait_budget_seconds must be a finite nonnegative number or None")
        self.base_seconds = float(base_seconds)
        self.cap_seconds = float(cap_seconds)
        self.budget = None if wait_budget_seconds is None else float(wait_budget_seconds)
        self.waited = 0.0
        self.exhausted = False

    @property
    def remaining(self) -> float | None:
        return None if self.budget is None else max(0.0, self.budget - self.waited)

    def __call__(self, attempt: int) -> float:
        return min(self.base_seconds * (PHI ** (attempt - 1)), self.cap_seconds) + random.uniform(0.0, 1.0)

    def next_delay(self, attempt: int, retry_after: float = 0.0) -> float | None:
        computed = self(attempt)
        delay = max(computed, float(retry_after or 0.0), MIN_INTERVAL)
        if self.budget is not None and delay > (self.remaining or 0.0) + 1e-6:
            self.exhausted = True
            return None
        return delay


@contextmanager
def golden_ratio_backoff_policy(
    base_seconds: float = 8.0,
    cap_seconds: float = 60.0,
    wait_budget_seconds: float | None = None,
):
    """Opt-in Golden-Ratio / Fibonacci backoff schedule w_k = min(base * phi^(k-1), cap) + U(0, 1)."""
    policy = _BackoffPolicy(
        base_seconds=base_seconds,
        cap_seconds=cap_seconds,
        wait_budget_seconds=wait_budget_seconds,
    )
    token = _BACKOFF_SCHEDULE.set(policy)
    try:
        yield policy
    finally:
        _BACKOFF_SCHEDULE.reset(token)


class ArxivCooldownError(RuntimeError):
    """A persisted server deadline forbids a new attempt; no request was sent."""

    def __init__(self, retry_at_utc):
        self.retry_at_utc = retry_at_utc
        super().__init__("arXiv cooldown active; may continue at " + retry_at_utc + " (UTC)")


@contextmanager
def record_requests(callback):
    """Optional Batch audit sink; write failures propagate before another HTTP."""
    token = _REQUEST_LOG.set(callback)
    try:
        yield
    finally:
        _REQUEST_LOG.reset(token)


def _utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value):
    if not isinstance(value, str):
        raise ValueError("Invalid HTTP log timestamp")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None or date.utcoffset() != timedelta(0):
        raise ValueError("HTTP log timestamp must be UTC")
    return date


def _retry_deadline(value, received_at_utc):
    """Server deadline only; absent/invalid headers never invent a cooldown."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"[0-9]+", value):
            date = _parse_utc(received_at_utc) + timedelta(seconds=int(value))
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        return None


def _request_event(url, attempt):
    # Whitelist arXiv endpoints/IDs, never serialize arbitrary query strings,
    # request headers, response bodies, User-Agent or exception messages.
    parts = urlsplit(url)
    endpoint, identifier = None, None
    if parts.scheme == "https" and parts.netloc == "export.arxiv.org":
        if parts.path == "/api/query":
            endpoint = API_URL
            ids = parse_qs(parts.query).get("id_list", [])
            if len(ids) == 1 and ID_PATTERN.fullmatch(ids[0]):
                identifier = ids[0]
        elif parts.path.startswith("/src/") and ID_PATTERN.fullmatch(unquote(parts.path[5:])):
            endpoint = "https://export.arxiv.org" + parts.path
            identifier = unquote(parts.path[5:])
    return dict(request_started_at_utc=_utc(time.time()), response_received_at_utc=None,
                endpoint=endpoint, query_identifier=identifier, attempt=attempt,
                http_status=None, retry_after=None, cooldown_deadline_utc=None)


def _response_event(event, status, headers):
    if event is not None and event["response_received_at_utc"] is None:
        received = _utc(time.time())
        raw = headers.get("Retry-After") if headers else None
        event.update(response_received_at_utc=received, http_status=status,
                     retry_after=raw, cooldown_deadline_utc=_retry_deadline(raw, received))


def _retry_after(value: str | None) -> float:
    if value is None:
        return 0.0
    value = value.strip()
    try:
        if re.fullmatch(r"[0-9]+", value):
            delay = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - time.time()
        return max(0.0, delay) if math.isfinite(delay) else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _retry_wait_deadline(server, received, *, fallback_seconds=None):
    if fallback_seconds is None:
        fallback_seconds = RETRY_429_FALLBACK_SECONDS
    if (type(fallback_seconds) not in (int, float)
            or not math.isfinite(fallback_seconds) or fallback_seconds < MIN_INTERVAL):
        raise ValueError("Invalid recorded arXiv 429 fallback")
    stamp = _parse_utc(received).timestamp()
    # Keep server evidence unchanged; the effective wait also honors pacing
    # after a runtime restart, including valid Retry-After: 0 or a past date.
    return _utc(max(_parse_utc(server).timestamp(), stamp + MIN_INTERVAL)
                if server is not None else stamp + fallback_seconds)


def _plan_429(event, attempt, policy):
    """Plan/persist evidence before the existing limiter performs any sleep."""
    global _next_request_at
    server = event["cooldown_deadline_utc"]
    deadline = _retry_wait_deadline(server, event["response_received_at_utc"])
    delay = max(0.0, _parse_utc(deadline).timestamp() - time.time())
    _next_request_at = max(_next_request_at, time.monotonic() + delay)
    planned = max(0.0, _next_request_at - time.monotonic())
    decision = "retry_once"
    if attempt == 2:
        decision = "retry_exhausted"
    elif planned > policy.remaining:
        decision = "budget_exceeded"
        policy.exhausted = True
    event.update(retry_wait_source="retry_after" if server is not None else "internal_backoff",
                 retry_wait_deadline_utc=deadline, retry_wait_seconds=planned,
                 retry_429_decision=decision)


def _network_status(error: Exception) -> str:
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, TimeoutError):
        return "timeout"
    if isinstance(reason, socket.gaierror):
        return "dns_error"
    if isinstance(reason, ssl.SSLError):
        return "tls_error"
    return "connection_error"


def _request_arxiv(
    url: str,
    *,
    timeout: float,
    max_attempts: int,
    user_agent: str,
    accept: str,
    read_response,
) -> dict:
    global _next_request_at

    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be a positive finite number")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    if (not isinstance(user_agent, str) or not user_agent.strip()
            or not user_agent.isascii() or "\r" in user_agent or "\n" in user_agent):
        raise ValueError("user_agent must be a non-empty ASCII header without newlines")

    policy = _RETRY_429.get()
    if policy is not None and (max_attempts != 1 or _REQUEST_LOG.get() is None):
        raise ValueError("Experimental 429 retry requires max_attempts=1 and durable HTTP logging")

    request = Request(url, headers={
        "User-Agent": user_agent,
        "Accept": accept,
        "Connection": "close",
    })
    with _request_lock:
        last_failure = None
        backoff_policy = _BACKOFF_SCHEDULE.get()
        for attempt in range(1, (2 if policy is not None else max_attempts) + 1):
            while (delay := _next_request_at - time.monotonic()) > 0:
                if policy is not None and attempt == 2 and delay > policy.remaining + 1e-6:
                    policy.exhausted = True
                    return last_failure
                if (
                    isinstance(backoff_policy, _BackoffPolicy)
                    and backoff_policy.budget is not None
                    and attempt > 1
                    and delay > (backoff_policy.remaining or 0.0) + 1e-6
                ):
                    backoff_policy.exhausted = True
                    return last_failure
                started_wait = time.monotonic()
                try:
                    time.sleep(delay)
                finally:
                    elapsed = max(0.0, time.monotonic() - started_wait)
                    http_metrics.waited("arxiv", elapsed)
                    if policy is not None and attempt == 2:
                        policy.waited += elapsed
                    if isinstance(backoff_policy, _BackoffPolicy) and attempt > 1:
                        backoff_policy.waited += elapsed
                if policy is not None and attempt == 2 and policy.waited > policy.budget + 1e-6:
                    policy.exhausted = True
                    return last_failure
                if (
                    isinstance(backoff_policy, _BackoffPolicy)
                    and backoff_policy.budget is not None
                    and attempt > 1
                    and backoff_policy.waited > backoff_policy.budget + 1e-6
                ):
                    backoff_policy.exhausted = True
                    return last_failure
            http_status = None
            retry_after = 0.0
            retry = False
            sink = _REQUEST_LOG.get()
            event = _request_event(url, attempt) if sink is not None else None
            event_flushed = False
            try:
                with http_metrics.request("arxiv", attempt) as observe:
                    with _opener.open(request, timeout=timeout) as response:
                        http_status = response.status
                        observe(http_status)
                        _response_event(event, http_status, response.headers)
                        retry_after = _retry_after(response.headers.get("Retry-After"))
                        if http_status != 200:
                            raise HTTPError(url, http_status, "Unexpected status", response.headers, None)
                        _next_request_at = max(
                            _next_request_at, time.monotonic() + max(MIN_INTERVAL, retry_after)
                        )
                        if sink is not None:
                            sink(event)
                            event_flushed = True
                        result = read_response(response)
                    extra_attempts = result.pop("_extra_attempts", 0) if isinstance(result, dict) else 0
                    if result.get("http_status") is None:
                        result["http_status"] = http_status
                    result["attempts"] = attempt + extra_attempts
                    return result
            except HTTPError as exc:
                http_status = exc.code
                try:
                    _response_event(event, http_status, exc.headers)
                    retry_after = _retry_after(exc.headers.get("Retry-After")) if exc.headers else 0.0
                    status = ("rate_limited" if exc.code == 429 else
                              "server_error" if 500 <= exc.code < 600 else "http_error")
                    error = str(exc)
                    retry = exc.code == 429 or 500 <= exc.code < 600
                finally:
                    exc.close()
            except (URLError, OSError, HTTPException) as exc:
                status = _network_status(exc)
                error = str(exc)
                retry = status != "tls_error"
            finally:
                _next_request_at = max(
                    _next_request_at, time.monotonic() + max(MIN_INTERVAL, retry_after)
                )
                if policy is not None and http_status == 429:
                    _plan_429(event, attempt, policy)
                if sink is not None and not event_flushed:
                    sink(event)

            last_failure = {
                "status": status, "http_status": http_status,
                "attempts": attempt, "error": error,
            }
            if policy is not None:
                if http_status != 429 or event["retry_429_decision"] != "retry_once":
                    return last_failure
                continue  # Same Request object; waiting stays in the shared limiter.
            if not retry or attempt == max_attempts:
                return last_failure
            if backoff_policy is not None:
                if isinstance(backoff_policy, _BackoffPolicy):
                    planned = backoff_policy.next_delay(attempt, retry_after)
                    if planned is None:
                        return last_failure
                    backoff = planned
                else:
                    backoff = max(float(backoff_policy(attempt)), retry_after)
            else:
                backoff = max(min(3.0 * 2 ** min(attempt - 1, 5), 60.0) + random.uniform(0.0, 1.0), retry_after)
            _next_request_at = max(_next_request_at, time.monotonic() + backoff)


def _request_arxiv_range(
    url: str,
    *,
    start_byte: int,
    total_expected: int,
    timeout: float,
    user_agent: str,
    attempt_number: int,
    read_chunk,
) -> dict:
    """Execute one paced, audited HTTP Range request while _request_lock is held."""
    global _next_request_at
    backoff_policy = _BACKOFF_SCHEDULE.get()
    while (delay := _next_request_at - time.monotonic()) > 0:
        if (
            isinstance(backoff_policy, _BackoffPolicy)
            and backoff_policy.budget is not None
            and delay > (backoff_policy.remaining or 0.0) + 1e-6
        ):
            backoff_policy.exhausted = True
            return {"status": "rate_limited", "http_status": 429, "error": "Retry wait budget exhausted"}
        started_wait = time.monotonic()
        try:
            time.sleep(delay)
        finally:
            elapsed = max(0.0, time.monotonic() - started_wait)
            http_metrics.waited("arxiv", elapsed)
            if isinstance(backoff_policy, _BackoffPolicy):
                backoff_policy.waited += elapsed

    range_req = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/octet-stream",
            "Range": f"bytes={start_byte}-{total_expected - 1}",
            "Connection": "close",
        },
    )
    http_status = None
    retry_after = 0.0
    sink = _REQUEST_LOG.get()
    event = _request_event(url, attempt_number) if sink is not None else None
    try:
        with http_metrics.request("arxiv", attempt_number) as observe:
            with _opener.open(range_req, timeout=timeout) as response:
                http_status = response.status
                observe(http_status)
                _response_event(event, http_status, response.headers)
                retry_after = _retry_after(response.headers.get("Retry-After"))
                if http_status != 206:
                    raise HTTPError(url, http_status, "Unexpected range status", response.headers, None)
                chunk_result = read_chunk(response)
                return {"http_status": http_status, **chunk_result}
    except HTTPError as exc:
        http_status = exc.code
        try:
            _response_event(event, http_status, exc.headers)
            retry_after = _retry_after(exc.headers.get("Retry-After")) if exc.headers else 0.0
            status = (
                "rate_limited" if exc.code == 429 else
                "server_error" if 500 <= exc.code < 600 else "http_error"
            )
            return {"status": status, "http_status": http_status, "error": str(exc)}
        finally:
            exc.close()
    except (URLError, OSError, HTTPException) as exc:
        return {"status": _network_status(exc), "http_status": http_status, "error": str(exc)}
    finally:
        _next_request_at = max(
            _next_request_at, time.monotonic() + max(MIN_INTERVAL, retry_after)
        )
        if sink is not None:
            sink(event)


def query_arxiv(
    mssv: str,
    arxiv_id: str,
    *,
    timeout: float = 30.0,
    max_attempts: int = 3,
    user_agent: str = USER_AGENT,
) -> dict:
    if not isinstance(mssv, str) or not re.fullmatch(r"[0-9]+", mssv):
        raise ValueError("mssv must be a non-empty string of digits")
    match = ID_PATTERN.fullmatch(arxiv_id) if isinstance(arxiv_id, str) else None
    if match is None or match.group("version") is not None:
        raise ValueError("arxiv_id must be a base ID without a version suffix")

    def read_xml(response):
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type.strip().lower() not in XML_CONTENT_TYPES:
            return {
                "status": "invalid_response",
                "error": f"Expected Atom/XML Content-Type, received {content_type!r}",
            }
        return parse_arxiv_response(response.read(), mssv, arxiv_id)

    url = API_URL + "?" + urlencode({"id_list": arxiv_id, "start": 0, "max_results": 1})
    result = _request_arxiv(
        url, timeout=timeout, max_attempts=max_attempts, user_agent=user_agent,
        accept="application/atom+xml", read_response=read_xml,
    )
    return {"mssv": mssv, "arxiv_id": arxiv_id, **result}
