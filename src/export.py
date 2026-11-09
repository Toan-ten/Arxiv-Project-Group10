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
    Maps ref_submission_date using arXiv submission_date if present, falling back
    to publication_date / year to avoid null values when Semantic Scholar only provides
    publication dates (DATA-01 resolution).

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

                # DATA-01 resolution: prefer exact submission_date, fallback to publication_date
                ref_sub_date = item.get("submission_date") or item.get("publication_date")
                if not ref_sub_date and item.get("year"):
                    ref_sub_date = f"{item['year']}-01-01"

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
