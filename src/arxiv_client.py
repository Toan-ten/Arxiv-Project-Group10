import math
import random
import re
import socket
import ssl
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from src.arxiv_discovery import ID_PATTERN, parse_arxiv_response


API_URL = "https://export.arxiv.org/api/query"
USER_AGENT = (
    "Arxiv-Project-Group10/0.1 "
    "(+https://github.com/Toan-ten/Arxiv-Project-Group10)"
)
MIN_INTERVAL = 3.0
XML_CONTENT_TYPES = {"application/atom+xml", "application/xml", "text/xml"}


class _NoRedirect(HTTPRedirectHandler):
    # urllib redirects would send another request without waiting.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(_NoRedirect())
_request_lock = Lock()
_next_request_at = 0.0


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

    request = Request(url, headers={
        "User-Agent": user_agent,
        "Accept": accept,
        "Connection": "close",
    })
    with _request_lock:
        for attempt in range(1, max_attempts + 1):
            while (delay := _next_request_at - time.monotonic()) > 0:
                time.sleep(delay)
            http_status = None
            retry_after = 0.0
            retry = False
            try:
                with _opener.open(request, timeout=timeout) as response:
                    http_status = response.status
                    retry_after = _retry_after(response.headers.get("Retry-After"))
                    if http_status != 200:
                        raise HTTPError(url, http_status, "Unexpected status", response.headers, None)
                    result = read_response(response)
                result.update(http_status=http_status, attempts=attempt)
                return result
            except HTTPError as exc:
                http_status = exc.code
                try:
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

            if not retry or attempt == max_attempts:
                return {
                    "status": status, "http_status": http_status,
                    "attempts": attempt, "error": error,
                }
            backoff = min(3.0 * 2 ** min(attempt - 1, 5), 60.0) + random.uniform(0.0, 1.0)
            _next_request_at = max(_next_request_at, time.monotonic() + backoff)


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
