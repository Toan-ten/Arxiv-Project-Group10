"""Read M1 references and M2 BibTeX from Parquet for M3 matching."""
from __future__ import annotations

import glob
import os

import pyarrow.parquet as pq


def _read_parquet(path):
    table = pq.read_table(path)
    return table.to_pylist() if hasattr(table, "to_pylist") else [
        {col: table.column(col)[i].as_py() for col in table.column_names}
        for i in range(table.num_rows)
    ]


def load_references(path):
    """Load M1 references Parquet. Returns list of dicts."""
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "**", "*.parquet"), recursive=True))
        if not files:
            raise FileNotFoundError("No parquet files in " + path)
        rows = []
        for f in files:
            rows.extend(_read_parquet(f))
        return rows
    return _read_parquet(path)


def load_bibtex(path):
    """Load M2 BibTeX Parquet. Returns list of dicts."""
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "**", "*.parquet"), recursive=True))
        if not files:
            raise FileNotFoundError("No parquet files in " + path)
        rows = []
        for f in files:
            rows.extend(_read_parquet(f))
        return rows
    return _read_parquet(path)


def group_by_arxiv(rows):
    out = {}
    for r in rows or []:
        aid = r.get("arxiv_id")
        if aid is None:
            continue
        out.setdefault(aid, []).append(r)
    return out


def join_publications(refs_rows, bibtex_rows):
    """Return dict arxiv_id -> (bibtex_list, refs_list)."""
    refs_by = group_by_arxiv(refs_rows)
    bib_by = group_by_arxiv(bibtex_rows)
    aids = sorted(set(refs_by) | set(bib_by))
    return {aid: (bib_by.get(aid, []), refs_by.get(aid, [])) for aid in aids}
