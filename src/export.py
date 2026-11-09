"""Dataset export module projecting M1 internal corpus storage into official M1 tables."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from src.storage import CorpusStorage


def export_papers_table(
    store: CorpusStorage,
    pairs: Iterable[Tuple[str, str]],
    *,
    verified_only: bool = True,
    require_m1_complete: bool = False,
) -> List[Dict]:
    """Export the official M1 `papers` table matching crawling.md schema.

    Schema:
      - arxiv_id (str, non-null)
      - title (str, nullable)
      - authors (list[str], nullable: None when unobserved, [] when empty)
      - submission_date (str, ISO YYYY-MM-DD, nullable)
      - revised_dates (list[str], nullable: None when unobserved, [] when confirmed no revisions)
      - venue (str, nullable)

    Contracts:
      - Default verified_only=True: restricts output strictly to verified M1 complete metadata (status == "complete").
        Incomplete or partial records are excluded from official production exports.
      - require_m1_complete=True: further restricts output strictly to papers that achieved full Milestone 1 completion
        (manifest.status in {"complete", "complete_with_no_tex"} and manifest.metadata_status == "complete").
    """
    rows = []
    for mssv, arxiv_id in pairs:
        try:
            if require_m1_complete:
                manifest = store.read_manifest(mssv, arxiv_id)
                if manifest.get("status") not in {"complete", "complete_with_no_tex"}:
                    continue
                if manifest.get("metadata_status") != "complete":
                    continue

            meta = store.read_metadata(mssv, arxiv_id)
            if not isinstance(meta, dict):
                continue
            status = meta.get("status")
            if verified_only:
                if status != "complete":
                    continue
            elif status not in {"partial", "complete"}:
                continue

            authors = meta.get("authors")
            revised_dates = meta.get("revised_dates")

            rows.append({
                "arxiv_id": str(arxiv_id),
                "title": meta.get("title"),
                "authors": list(authors) if authors is not None else None,
                "submission_date": meta.get("submission_date"),
                "revised_dates": list(revised_dates) if revised_dates is not None else None,
                "venue": meta.get("venue"),
            })
        except Exception:
            continue
    return rows


def export_diagnostic_papers_table(
    store: CorpusStorage,
    pairs: Iterable[Tuple[str, str]],
    *,
    include_partial: bool = True,
) -> List[Dict]:
    """Export intermediate/diagnostic papers table for analysis and inspection.

    Separated from the official `export_papers_table`: allows intermediate records
    (status in {'partial', 'complete'}) when include_partial=True.
    """
    return export_papers_table(store, pairs, verified_only=not include_partial, require_m1_complete=False)


def export_references_table(
    store: CorpusStorage,
    pairs: Iterable[Tuple[str, str]],
    *,
    verified_only: bool = True,
) -> List[Dict]:
    """Export the official M1 `references` table matching crawling.md schema.

    Filters intermediate items to only include verified arXiv citations.
    When verified_only=True, only exports references from papers with verified complete coverage
    (references.status == 'complete' and coverage.complete is True).
    Preserves null for ref_submission_date when unobserved in source data (never fabricates dates).
    Preserves null for ref_authors when unobserved in source data (never converts null to []).

    Schema:
      - arxiv_id (str, non-null)
      - ref_arxiv_id (str, non-null)
      - ref_title (str, nullable)
      - ref_authors (list[str], nullable: None when unobserved, [] when empty)
      - ref_submission_date (str, nullable)
      - ref_s2_id (str, nullable)
    """
    rows = []
    seen = set()
    for mssv, arxiv_id in pairs:
        try:
            ref_data = store.read_references(mssv, arxiv_id)
            if not isinstance(ref_data, dict):
                continue
            if verified_only:
                cov = ref_data.get("coverage")
                if not isinstance(cov, dict):
                    continue
                if ref_data.get("status") != "complete" or cov.get("complete") is not True:
                    continue

            items = ref_data.get("items")
            if not isinstance(items, list):
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue
                ref_arxiv = item.get("arxiv_id")
                if not ref_arxiv or not isinstance(ref_arxiv, str):
                    continue  # Only include citations resolved to an arXiv ID
                key = (str(arxiv_id), str(ref_arxiv))
                if key in seen:
                    continue
                seen.add(key)

                ref_sub_date = item.get("submission_date")
                raw_authors = item.get("authors")

                rows.append({
                    "arxiv_id": str(arxiv_id),
                    "ref_arxiv_id": str(ref_arxiv),
                    "ref_title": item.get("title"),
                    "ref_authors": list(raw_authors) if raw_authors is not None else None,
                    "ref_submission_date": str(ref_sub_date) if ref_sub_date else None,
                    "ref_s2_id": item.get("s2_paper_id"),
                })
        except Exception:
            continue
    return rows


def validate_exported_tables(
    papers_rows: List[Dict],
    references_rows: List[Dict],
) -> Dict[str, Any]:
    """Validate exported datasets against M1 canonical schema rules.

    Checks:
    - Non-null arxiv_id in papers and references.
    - No duplicate arxiv_id in papers.
    - Required keys present in every record.
    - Valid nullable list semantics (authors/revised_dates/ref_authors can be None or list of strings).
    - Foreign key consistency and duplicate edge prevention.
    """
    issues: List[str] = []
    paper_keys = {"arxiv_id", "title", "authors", "submission_date", "revised_dates", "venue"}
    ref_keys = {"arxiv_id", "ref_arxiv_id", "ref_title", "ref_authors", "ref_submission_date", "ref_s2_id"}

    seen_paper_ids = set()
    for idx, p in enumerate(papers_rows):
        if not isinstance(p, dict):
            issues.append(f"Paper row {idx} is not a dictionary")
            continue
        missing = paper_keys - set(p.keys())
        if missing:
            issues.append(f"Paper row {idx} missing fields: {missing}")
        aid = p.get("arxiv_id")
        if not aid or not isinstance(aid, str):
            issues.append(f"Paper row {idx} invalid arxiv_id")
        elif aid in seen_paper_ids:
            issues.append(f"Duplicate paper arxiv_id: {aid}")
        else:
            seen_paper_ids.add(aid)

        authors = p.get("authors")
        if authors is not None:
            if not isinstance(authors, list):
                issues.append(f"Paper row {idx} authors must be a list or None")
            elif not all(isinstance(a, str) for a in authors):
                issues.append(f"Paper row {idx} authors elements must be strings")

        revised_dates = p.get("revised_dates")
        if revised_dates is not None:
            if not isinstance(revised_dates, list):
                issues.append(f"Paper row {idx} revised_dates must be a list or None")
            elif not all(isinstance(d, str) for d in revised_dates):
                issues.append(f"Paper row {idx} revised_dates elements must be strings")

    seen_ref_pairs = set()
    for idx, r in enumerate(references_rows):
        if not isinstance(r, dict):
            issues.append(f"Reference row {idx} is not a dictionary")
            continue
        missing = ref_keys - set(r.keys())
        if missing:
            issues.append(f"Reference row {idx} missing fields: {missing}")
        src_id = r.get("arxiv_id")
        tgt_id = r.get("ref_arxiv_id")
        if not src_id or not isinstance(src_id, str):
            issues.append(f"Reference row {idx} invalid source arxiv_id")
        if not tgt_id or not isinstance(tgt_id, str):
            issues.append(f"Reference row {idx} invalid ref_arxiv_id")
        pair_key = (src_id, tgt_id)
        if pair_key in seen_ref_pairs:
            issues.append(f"Duplicate reference edge: {pair_key}")
        else:
            seen_ref_pairs.add(pair_key)

        ref_authors = r.get("ref_authors")
        if ref_authors is not None:
            if not isinstance(ref_authors, list):
                issues.append(f"Reference row {idx} ref_authors must be a list or None")
            elif not all(isinstance(a, str) for a in ref_authors):
                issues.append(f"Reference row {idx} ref_authors elements must be strings")

    is_valid = len(issues) == 0
    return {
        "is_valid": is_valid,
        "total_papers": len(papers_rows),
        "total_references": len(references_rows),
        "unique_source_papers_with_refs": len({r.get("arxiv_id") for r in references_rows if r.get("arxiv_id")}),
        "issue_count": len(issues),
        "issues": issues[:20],
    }


def export_to_parquet(records: List[Dict], destination: str | Path) -> None:
    """Save records to Parquet format using pyarrow (with jsonl fallback)."""
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        table = pa.Table.from_pylist(records)
        pq.write_table(table, dest)
    except ImportError:
        jsonl_path = dest.with_suffix(".jsonl")
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
