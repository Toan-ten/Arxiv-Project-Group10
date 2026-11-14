"""Pure reference-quality assessment, independent of API pagination status."""
from __future__ import annotations


def assess_references(metadata: dict | None, snapshot: dict | None) -> dict:
    """Describe observed counts without claiming complete source bibliography."""
    paper = (metadata or {}).get("paper") or {}
    count = paper.get("referenceCount")
    valid_count = type(count) is int and count >= 0
    snapshot = snapshot or {}
    items = snapshot.get("items") or []
    coverage = snapshot.get("coverage") or {}
    strategy = (snapshot.get("provenance") or {}).get("retrieval_strategy", "paginated")
    complete = snapshot.get("status") == "complete" and coverage.get("complete") is True
    count_match = count == len(items) if valid_count and complete else None
    warnings = []
    if complete and not valid_count:
        warnings.append("metadata_reference_count_unknown")
    if count_match is False:
        warnings.append("metadata_reference_count_mismatch")
    arxiv_items = [item for item in items if item.get("arxiv_id")]
    missing_dates = [item["arxiv_id"] for item in arxiv_items
                     if not (item.get("submission_date") or item.get("publication_date"))]
    return {
        "schema_version": 1,
        "retrieval_strategy": strategy,
        "collection_complete": complete,
        "pagination_complete": complete if strategy == "paginated" else None,
        "metadata_reference_count": count if valid_count else None,
        "retrieved_count": len(items),
        "count_match": count_match,
        "fallback_reason": ((metadata or {}).get("batch_fallback") or {}).get("reason"),
        "warnings": warnings,
        "arxiv_reference_rows": len(arxiv_items),
        "missing_date_rows": len(missing_dates),
        "missing_date_ids": sorted(set(missing_dates)),
    }


def summarize_quality(assessments: dict[str, dict]) -> dict:
    """Aggregate row counts and unique missing-date IDs separately."""
    values = list(assessments.values())
    return {
        "papers_assessed": len(values),
        "collection_complete_papers": sum(v["collection_complete"] for v in values),
        "count_mismatch_papers": sum(v["count_match"] is False for v in values),
        "count_unknown_papers": sum(v["count_match"] is None for v in values),
        "missing_date_rows": sum(v["missing_date_rows"] for v in values),
        "missing_date_unique_ids": len({aid for v in values for aid in v["missing_date_ids"]}),
        "papers": assessments,
        "scope": "S2 observations; count agreement does not prove LaTeX bibliography coverage",
    }
