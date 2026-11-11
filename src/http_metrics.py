"""Transport-attempt counters scoped to one sequential Batch invocation.

No URLs, payloads, identifiers, headers or exception messages are retained.
These count opener calls, not proof that a server received the request.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import math
from urllib.error import HTTPError

PROVIDERS = ("arxiv", "semantic_scholar")
COUNTS = ("attempts", "completed_attempts", "retry_attempts", "http_errors", "without_http_response")
_CURRENT = ContextVar("group10_http_metrics", default=None)


def _empty():
    return dict(**{name: 0 for name in COUNTS}, status_counts={}, wait_seconds=0.0)


def _sum(values):
    result = _empty()
    for value in values:
        for name in COUNTS:
            result[name] += value[name]
        result["wait_seconds"] += value["wait_seconds"]
        for status, count in value["status_counts"].items():
            result["status_counts"][status] = result["status_counts"].get(status, 0) + count
    return result


class HttpMetrics:
    def __init__(self):
        self.providers = {name: _empty() for name in PROVIDERS}

    def snapshot(self, *, complete):
        providers = copy.deepcopy(self.providers)
        total = _sum(providers.values())
        return dict(schema_version=1,
                    status="collected" if complete and total["attempts"] == total["completed_attempts"] else "partial",
                    providers=providers, total=total)


@contextmanager
def collect(collector):
    token = _CURRENT.set(collector)
    try:
        yield collector
    finally:
        _CURRENT.reset(token)


@contextmanager
def request(provider, attempt):
    collector = _CURRENT.get()
    record = None if collector is None else collector.providers[provider]
    status, interrupted = None, False
    if record is not None:
        record["attempts"] += 1
        record["retry_attempts"] += int(attempt > 1)

    def observe(code):
        nonlocal status
        if type(code) is int and 100 <= code <= 599:
            status = code

    try:
        yield observe
    except BaseException as exc:
        if isinstance(exc, HTTPError):
            observe(exc.code)
        interrupted = not isinstance(exc, Exception)
        raise
    finally:
        if record is not None:
            if not interrupted:
                record["completed_attempts"] += 1
                if status is None:
                    record["without_http_response"] += 1
            if status is not None and not interrupted:
                key = str(status)
                record["status_counts"][key] = record["status_counts"].get(key, 0) + 1
                record["http_errors"] += int(status >= 400)


def waited(provider, seconds):
    collector = _CURRENT.get()
    if collector is not None and math.isfinite(seconds) and seconds >= 0:
        collector.providers[provider]["wait_seconds"] += seconds


def _valid_record(value):
    if not isinstance(value, dict) or set(value) != set(_empty()):
        raise ValueError("Invalid HTTP metrics")
    if any(type(value[k]) is not int or value[k] < 0 for k in COUNTS):
        raise ValueError("Invalid HTTP metrics")
    statuses = value["status_counts"]
    if (not isinstance(statuses, dict) or any(
            not isinstance(k, str) or len(k) != 3 or not k.isascii() or not k.isdigit()
            or not 100 <= int(k) <= 599 or type(v) is not int or v < 0
            for k, v in statuses.items())):
        raise ValueError("Invalid HTTP metrics")
    if (type(value["wait_seconds"]) not in (int, float)
            or not math.isfinite(value["wait_seconds"]) or value["wait_seconds"] < 0
            or value["completed_attempts"] > value["attempts"]
            or value["retry_attempts"] > value["attempts"]
            or sum(statuses.values()) + value["without_http_response"] != value["completed_attempts"]
            or value["http_errors"] != sum(v for k, v in statuses.items() if int(k) >= 400)):
        raise ValueError("Invalid HTTP metrics")


def validate(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "status", "providers", "total"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["status"], str) or value["status"] not in {"collected", "partial"}
            or not isinstance(value["providers"], dict) or set(value["providers"]) != set(PROVIDERS)):
        raise ValueError("Invalid HTTP metrics")
    for record in [*value["providers"].values(), value["total"]]:
        _valid_record(record)
    expected = _sum(value["providers"].values())
    if (any(expected[k] != value["total"][k] for k in (*COUNTS, "status_counts"))
            or not math.isclose(expected["wait_seconds"], value["total"]["wait_seconds"], abs_tol=1e-8)
            or value["status"] == "collected"
            and value["total"]["attempts"] != value["total"]["completed_attempts"]):
        raise ValueError("Invalid HTTP metrics")
    return copy.deepcopy(value)


def summarize(attempts):
    values, missing, invalid, partial = [], [], [], []
    seen = set()
    for attempt in attempts:
        identity = attempt["attempt_id"]
        if identity in seen:
            invalid.append(identity)
            continue
        seen.add(identity)
        if "http_metrics" not in attempt:
            missing.append(identity)
            continue
        try:
            value = validate(attempt["http_metrics"])
        except (ValueError, TypeError, KeyError, OverflowError):
            invalid.append(identity)
            continue
        values.append(value)
        if value["status"] != "collected" or attempt["status"] == "running":
            partial.append(identity)
    complete = bool(attempts) and not missing and not invalid and not partial
    providers = {name: _sum(v["providers"][name] for v in values) for name in PROVIDERS}
    known = _sum(providers.values())
    status = "collected" if complete else "partial" if values else "not_collected"
    if invalid:
        status = "invalid"
    return dict(
        http_requests=known["attempts"] if complete else None,
        http_errors=known["http_errors"] if complete else None,
        http_metrics_status=status,
        http_metrics=dict(
            known=known, providers=providers, missing_attempt_ids=missing,
            invalid_attempt_ids=invalid, partial_attempt_ids=partial,
            scope="sum of distinct logged attempts; legacy/unflushed attempts are not reconstructed",
            wait_scope="measured limiter sleeps; S2 pacing/backoff overlap counted once",
        ),
    )
