"""Identity-checked S2 paper batches with the reference fields required by M1.

Transport, retries and normalization remain owned by semantic_scholar. A nested
reference list is complete only when it agrees with an explicit referenceCount.
Relation annotations belong to the paginated endpoint and are not fabricated.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from urllib.parse import urlencode

from src import semantic_scholar as s2

PAPER_REFERENCE_FIELDS = tuple(
    field for field in s2.REFERENCE_FIELDS
    if field not in {"contexts", "intents", "isInfluential"}
)
BATCH_FIELDS = s2.METADATA_FIELDS + tuple(
    "references." + field for field in PAPER_REFERENCE_FIELDS
)
SIZE_ERRORS = {
    "response_too_large", "http_413_request_too_large", "http_400_response_too_large",
}


def _snapshot(identifier: str, paper: dict) -> tuple[dict | None, str | None]:
    references = paper.get("references")
    if not isinstance(references, list):
        return None, "references_missing_or_invalid"
    total = paper.get("referenceCount")
    if type(total) is not int or total < 0 or total != len(references):
        return None, "reference_count_unverified"
    items = []
    try:
        for reference in references:
            if reference is not None and not isinstance(reference, dict):
                return None, "invalid_nested_reference"
            items.append(s2._normalize_reference({"citedPaper": reference}))
    except (ValueError, TypeError, KeyError):
        return None, "invalid_nested_reference"
    return {
        "contract_version": 2, "arxiv_id": identifier, "status": "complete",
        "provenance": {
            "provider": "semantic_scholar", "query_identifier": "ARXIV:" + identifier,
            "s2_paper_id": None,
            "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "retrieval_strategy": "paper_batch", "fields_profile": "m1",
            "source_s2_paper_id": paper["paperId"],
            "relation_fields_collected": False,
        },
        "coverage": {"complete": True, "continuation": None,
                     "reported_total": total, "retrieved_count": len(items)},
        "items": items, "error": None,
    }, None


def fetch_enrichment(
    arxiv_ids: list[str], *, batch_size: int = 100,
    limiter: s2.RateLimiter | None = None, timeout: float = 30.0,
    max_attempts: int = 8, max_response_bytes: int = s2.MAX_RESPONSE_BYTES,
    max_retry_wait: float = 600.0, backoff_base: float = 3.0,
    backoff_cap: float = 60.0,
) -> list[dict]:
    """Return metadata plus verified M1 references, splitting size errors only.

    A None references value requests a paginated fallback, never an empty result.
    All identities in a response are verified before returning any of its rows.
    403/429 halt recursion/chunking; unsent rows have attempts=0. No disk writes.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be between 1 and 500")
    if not isinstance(arxiv_ids, (list, tuple)):
        raise ValueError("arxiv_ids must be an explicit list/tuple")
    identifiers = list(arxiv_ids)
    for identifier in identifiers:
        match = s2.ID_PATTERN.fullmatch(identifier) if isinstance(identifier, str) else None
        if match is None or match.group("version") is not None:
            raise ValueError("Expected base arXiv IDs")
    for value, name in ((timeout, "timeout"),):
        s2._positive(value, name)
    s2._retry_policy(max_retry_wait, backoff_base, backoff_cap)
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("Invalid max_attempts")
    if (type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= s2.MAX_RESPONSE_BYTES):
        raise ValueError("Invalid max_response_bytes")
    limiter = s2.DEFAULT_LIMITER if limiter is None else limiter
    if not isinstance(limiter, s2.RateLimiter):
        raise ValueError("Expected a shared RateLimiter")
    key = s2._key_from_environment()

    def row(identifier, response, paper=None, *, unsent=False):
        metadata = dict(
            arxiv_id=identifier, status=response["status"],
            http_status=None if unsent else response["http_status"],
            attempts=0 if unsent else response["attempts"],
            paper=None, error=response["error"],
        )
        snapshot, reason = None, None
        if unsent:
            metadata["error"] = "not_attempted_after_http_policy"
        elif response["payload"] is not None:
            metadata["status"] = "not_found" if paper is None else "found"
            if paper is not None:
                metadata["paper"] = {k: copy.deepcopy(v) for k, v in paper.items()
                                     if k != "references"}
                snapshot, reason = _snapshot(identifier, paper)
                if reason is not None:
                    metadata["batch_fallback"] = {
                        "reason": reason, "references_present": "references" in paper,
                        "references": copy.deepcopy(paper.get("references")),
                    }
        return dict(metadata=metadata, references=snapshot, fallback_reason=reason)

    def blocked(rows):
        return next((entry["metadata"] for entry in rows
                     if entry["metadata"]["http_status"] in {403, 429}), None)

    def request(chunk):
        response = s2._request_json(
            s2.BATCH_URL + "?" + urlencode({"fields": ",".join(BATCH_FIELDS)}),
            method="POST", body=json.dumps({"ids": ["ARXIV:" + i for i in chunk]}).encode(),
            validate=lambda value: s2._validate_batch(value, chunk, True),
            timeout=timeout, max_attempts=max_attempts, limiter=limiter, key=key,
            max_response_bytes=max_response_bytes, max_retry_wait=max_retry_wait,
            backoff_base=backoff_base, backoff_cap=backoff_cap,
        )
        if response["error"] in SIZE_ERRORS and len(chunk) > 1:
            middle = len(chunk) // 2
            left = request(chunk[:middle])
            stop = blocked(left)
            if stop is not None:
                placeholder = dict(stop, payload=None)
                return left + [row(i, placeholder, unsent=True) for i in chunk[middle:]]
            return left + request(chunk[middle:])
        return [row(identifier, response,
                    None if response["payload"] is None else response["payload"][i])
                for i, identifier in enumerate(chunk)]

    results = []
    for start in range(0, len(identifiers), batch_size):
        chunk = identifiers[start:start + batch_size]
        rows = request(chunk)
        results.extend(rows)
        stop = blocked(rows)
        if stop is not None:
            placeholder = dict(stop, payload=None)
            results.extend(row(i, placeholder, unsent=True)
                           for i in identifiers[start + len(chunk):])
            break
    return results
