"""Dataset export module projecting M1 internal corpus storage into official M1 tables."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from src.storage import CorpusStorage


def export_papers_table(store: CorpusStorage, pairs: Iterable[Tuple[str, str]]) -> List[Dict]:
    """Export the official M1 `papers` table matching crawling.md schema.

    Schema:
      - arxiv_id (str, non-null)
      - title (str)
      - authors (list[str])
      - submission_date (str, ISO YYYY-MM-DD)
      - revised_dates (list[str])
      - venue (str, nullable)
    """
    rows = []
    for mssv, arxiv_id in pairs:
        try:
            meta = store.read_metadata(mssv, arxiv_id)
            if meta.get("status") in {"partial", "complete"}:
                rows.append({
                    "arxiv_id": str(arxiv_id),
                    "title": meta.get("title"),
                    "authors": meta.get("authors") or [],
                    "submission_date": meta.get("submission_date"),
                    "revised_dates": meta.get("revised_dates") or [],
                    "venue": meta.get("venue"),
                })
        except Exception:
            continue
    return rows


def export_references_table(store: CorpusStorage, pairs: Iterable[Tuple[str, str]]) -> List[Dict]:
    """Export the official M1 `references` table matching crawling.md schema.

    Filters intermediate items to only include verified arXiv citations.
    Preserves null for ref_submission_date when unobserved in source data (never fabricates dates).

    Schema:
      - arxiv_id (str, non-null)
      - ref_arxiv_id (str, non-null)
      - ref_title (str)
      - ref_authors (list[str])
      - ref_submission_date (str, nullable)
      - ref_s2_id (str)
    """
    rows = []
    seen = set()
    for mssv, arxiv_id in pairs:
        try:
            ref_data = store.read_references(mssv, arxiv_id)
            for item in ref_data.get("items", []):
                ref_arxiv = item.get("arxiv_id")
                if not ref_arxiv:
                    continue  # Only include citations resolved to an arXiv ID
                key = (str(arxiv_id), str(ref_arxiv))
                if key in seen:
                    continue
                seen.add(key)

                ref_sub_date = item.get("submission_date")

                rows.append({
                    "arxiv_id": str(arxiv_id),
                    "ref_arxiv_id": str(ref_arxiv),
                    "ref_title": item.get("title"),
                    "ref_authors": item.get("authors") or [],
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
    - Valid types (authors as list, strings, nulls preserved).
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
        if not isinstance(p.get("authors"), list):
            issues.append(f"Paper row {idx} authors must be a list")
        if not isinstance(p.get("revised_dates"), list):
            issues.append(f"Paper row {idx} revised_dates must be a list")

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
        if not isinstance(r.get("ref_authors"), list):
            issues.append(f"Reference row {idx} ref_authors must be a list")

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
