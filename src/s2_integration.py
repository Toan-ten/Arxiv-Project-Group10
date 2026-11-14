"""S2/v2 adaptation and read-only cache validation; no HTTP or Storage writer.

The client owns identity parsing/normalization. Reusing its validators keeps
saved raw records under the same policy as fresh responses. One writer only.
"""
import copy
import json
from datetime import datetime, timezone

from src import semantic_scholar as s2
from src.storage import StorageError


def configuration(value):
    """None disables S2. An explicit dict enables it; secrets are never options."""
    if value is None:
        return None
    defaults = dict(timeout=30.0, max_attempts=1, min_interval=1.0,
                    reference_limit=100, max_pages=1000,
                    max_response_bytes=s2.MAX_RESPONSE_BYTES)
    policy_keys = {"max_retry_wait", "backoff_base", "backoff_cap"}
    if not isinstance(value, dict) or set(value) - (set(defaults) | policy_keys):
        raise ValueError("Invalid S2 configuration keys")
    result = dict(defaults, **value)
    for name in policy_keys & set(value):
        s2._positive(value[name], "s2." + name)
        result[name] = float(value[name])
    s2._retry_policy(result.get("max_retry_wait", 60.0),
                     result.get("backoff_base", 1.0), result.get("backoff_cap", 60.0))
    for name in ("timeout", "min_interval"):
        s2._positive(result[name], "s2." + name)
        result[name] = float(result[name])
    for name, maximum in (("max_attempts", None), ("reference_limit", 1000),
                          ("max_pages", None), ("max_response_bytes", s2.MAX_RESPONSE_BYTES)):
        n = result[name]
        if type(n) is not int or n < 1 or maximum is not None and n > maximum:
            raise ValueError("Invalid s2." + name)
    return result


def _require(condition, code):
    if not condition:
        raise StorageError("Invalid S2 evidence: " + code)


def _stamp(value):
    try:
        return (isinstance(value, str)
                and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None)
    except ValueError:
        return False


def _safe(value):
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise StorageError("Invalid S2 evidence: non_json") from None
    key = s2._key_from_environment()
    _require(key is None or not s2._contains_secret(value, key), "contains_secret")


def metadata_result(result, identifier):
    """Validate the envelope before treating any response/cache as evidence."""
    _safe(result)
    _require(isinstance(result, dict) and result.get("arxiv_id") == identifier,
             "metadata_identity")
    _require(isinstance(result.get("status"), str) and result["status"] in {
        "found", "not_found", "rate_limited", "http_error", "timeout", "invalid_response",
    }, "metadata_status")
    # Bộ giới hạn dùng lại có thể từ chối trước khi phát sinh request mới.
    zero_cooldown = (result.get("attempts") == 0
                     and result.get("error") == "cooldown_wait_budget_exceeded"
                     and result.get("status") in {"rate_limited", "http_error"})
    _require(type(result.get("attempts")) is int
             and (result["attempts"] >= 1 or zero_cooldown), "attempts")
    http = result.get("http_status")
    _require(http is None or type(http) is int and 100 <= http <= 599, "http_status")
    status = result["status"]
    if status in {"found", "not_found"}:
        _require(http == 200 and result.get("error") is None, "metadata_success")
        _require((status == "found") == (result.get("paper") is not None), "metadata_paper")
        if status == "found":
            try:
                s2._validate_batch([result["paper"]], [identifier], True)
            except (ValueError, TypeError, KeyError):
                raise StorageError("Invalid S2 evidence: metadata_identity_or_shape") from None
    else:
        _require(result.get("paper") is None and isinstance(result.get("error"), str),
                 "metadata_failure")
        _require(status != "rate_limited" or http == 429, "rate_limit_status")
        _require(http != 429 or status == "rate_limited", "rate_limit_status")
    fallback = result.get("batch_fallback")
    if fallback is not None:
        from src.s2_batch import _snapshot
        _require(status == "found" and isinstance(fallback, dict)
                 and type(fallback.get("references_present")) is bool,
                 "batch_fallback_shape")
        original = copy.deepcopy(result["paper"])
        if fallback["references_present"]:
            original["references"] = fallback.get("references")
        else:
            _require(fallback.get("references") is None, "batch_fallback_absent")
        _snapshot_value, reason = _snapshot(identifier, original)
        _require(reason is not None and reason == fallback.get("reason"), "batch_fallback_reason")
    return result


def metadata_cached(metadata):
    provenance = metadata["provenance"]["semantic_scholar"]
    if provenance is None:
        _require(metadata["status"] != "complete", "complete_metadata_without_s2")
        return False
    _require(isinstance(provenance, dict)
             and provenance.get("provider") == "semantic_scholar"
             and provenance.get("query_identifier") == "ARXIV:" + metadata["arxiv_id"]
             and _stamp(provenance.get("retrieved_at")), "metadata_provenance")
    result = metadata_result(provenance.get("snapshot"), metadata["arxiv_id"])
    _require(result["status"] != "found" or metadata["status"] in {"partial", "complete"},
             "found_metadata_stage")
    _require(metadata["status"] != "complete" or result["status"] == "found",
             "complete_metadata_without_found")
    return result["status"] == "found"


def _pristine_references(value):
    p, c = value["provenance"], value["coverage"]
    return (value["status"] in {"not_attempted", "in_progress", "interrupted"}
            and p == dict(provider="semantic_scholar", query_identifier=None,
                          s2_paper_id=None, retrieved_at=None)
            and c == dict(complete=False, continuation=None,
                          reported_total=None, retrieved_count=0)
            and value["items"] == [] and value.get("error") is None)


def references_evidence(value, identifier, *, for_resume=False):
    """Validate identity/raw normalization in addition to Storage's v2 schema."""
    _safe(value)
    _require(isinstance(value, dict) and value.get("arxiv_id") == identifier
             and value.get("contract_version") == 2, "reference_identity")
    # Bộ đọc Storage đã kiểm tra cấu trúc; bước này cũng kiểm tra response vừa nhận.
    from src.storage import CorpusStorage
    validator = object.__new__(CorpusStorage)
    validator.paper_path = lambda *_: None
    validator._references(value, "", identifier)
    if _pristine_references(value):
        return "fresh"
    p, c = value["provenance"], value["coverage"]
    _require(p["query_identifier"] == "ARXIV:" + identifier
             and p["s2_paper_id"] is None and _stamp(p["retrieved_at"]),
             "reference_provenance")
    for item in value["items"]:
        try:
            normalized = s2._normalize_reference(item["raw"])
        except (ValueError, TypeError, KeyError):
            raise StorageError("Invalid S2 evidence: reference_raw") from None
        _require(normalized == item, "reference_normalization")
    error = value.get("error")
    _require(error is None or isinstance(error, dict), "reference_error")
    if value["status"] == "complete":
        _require(error is None, "complete_references_with_error")
        return "cached"
    if value["status"] == "not_found":
        _require(not value["items"] and not c["complete"] and c["continuation"] is None
                 and isinstance(error, dict) and error.get("http_status") == 404,
                 "reference_not_found")
        return "fresh"
    if for_resume:
        previous = copy.deepcopy(value)
        if previous["status"] == "in_progress":
            previous["status"] = "partial" if previous["items"] else "failed"
        try:
            s2._validated_resume(identifier, previous, s2._key_from_environment())
        except ValueError:
            raise StorageError("Invalid S2 evidence: reference_resume") from None
    return "resumed"


def validate_checkpoint(store, mssv, identifier, *, for_resume=True):
    """Validate BOTH caches before recovery can write or any new request starts."""
    store.paper_path(mssv, identifier)
    metadata = store.read_metadata(mssv, identifier)
    references = store.read_references(mssv, identifier)
    metadata_cached(metadata)
    references_evidence(references, identifier, for_resume=for_resume)


def metadata_snapshot(metadata, result):
    """Preserve arXiv fields/dates/versions, filling only genuinely missing fields."""
    result = copy.deepcopy(metadata_result(result, metadata["arxiv_id"]))
    value = copy.deepcopy(metadata)
    value["provenance"]["semantic_scholar"] = dict(
        provider="semantic_scholar", query_identifier="ARXIV:" + metadata["arxiv_id"],
        retrieved_at=datetime.now(timezone.utc).isoformat(), snapshot=result,
    )
    if result["status"] != "found":
        value["status"] = "not_found" if result["status"] == "not_found" else "failed"
        return value
    paper = result["paper"]
    if not value["title"] and isinstance(paper.get("title"), str) and paper["title"].strip():
        value["title"] = paper["title"]
    authors = paper.get("authors")
    if not value["authors"] and authors and all(
            isinstance(a.get("name"), str) and a["name"].strip() for a in authors):
        value["authors"] = [a["name"] for a in authors]
    if value["venue"] is None and paper.get("venue"):
        value["venue"] = paper["venue"]
    missing = [f for f in ("title", "authors", "submission_date") if not value[f]]
    names, dates = value["versions"], value["revised_dates"]
    if not (value["version_status"] == "resolved" and names
            and all(v["date_status"] == "verified" for v in names)
            and value["submission_date"] == names[0]["date"]
            and dates is not None and dates == [v["date"] for v in names[1:]]
            and (len(names) == 1 or value["provenance"]["version_history"])):
        missing.append("version_history")
    value["missing_fields"] = missing
    value["status"] = "partial" if missing else "complete"
    return value


def reference_result(previous, result):
    """Refuse degraded/reordered snapshots without modifying saved evidence."""
    references_evidence(result, previous["arxiv_id"])
    error = result.get("error") or {}
    _require(not error.get("resume_reason")
             and error.get("message") not in {
                 "resume_prefix_mismatch", "resume_prefix_truncated", "resume_source_not_found",
             }, "reference_resume_conflict")
    _require(result["items"][:len(previous["items"])] == previous["items"],
             "reference_prefix")
    return result


def http_status(result, *, references=False):
    if references:
        return (result.get("error") or {}).get("http_status")
    return result.get("http_status")


def enrich_s2_batch_metadata(store, papers, s2_config=None, client=None, limiter=None):
    """Enrich S2 metadata in batch mode (up to 100 papers per single API call)."""
    if client is None:
        from src import semantic_scholar as client
    eligible = [
        p for p in papers
        if p.get("discovery", {}).get("status") == "found"
        and not p.get("blocked")
    ]
    if not eligible:
        return {}

    id_to_paper = {p["arxiv_id"]: p for p in eligible}
    id_list = list(id_to_paper.keys())

    results_map = {}
    options = dict(s2_config or {})
    for i in range(0, len(id_list), 100):
        chunk_ids = id_list[i:i + 100]
        if limiter is not None:
            options["limiter"] = limiter
        res = client.fetch_metadata(chunk_ids, **options)
        items = res.get("papers", []) if isinstance(res, dict) else res
        for item in items:
            aid = item.get("arxiv_id")
            if aid in id_to_paper:
                p = id_to_paper[aid]
                mssv = p["mssv"]
                if store is not None:
                    try:
                        meta = store.read_metadata(mssv, aid)
                        updated = metadata_snapshot(meta, item)
                        store.save_metadata(mssv, aid, updated)
                    except Exception:
                        pass
                results_map[aid] = item
    return results_map
