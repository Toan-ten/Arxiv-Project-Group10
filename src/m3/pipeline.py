"""M3 matching pipeline: run baseline matcher over corpus and write predictions."""
from __future__ import annotations

import json
import os

import pyarrow as pa
import pyarrow.parquet as pq

from .corpus import join_publications, load_bibtex, load_references
from .matching import build_candidates, mrr_at_k


PREDICTION_COLUMNS = [
    "arxiv_id",
    "citation_key",
    "rank",
    "ref_arxiv_id",
    "score",
    "ref_title",
]


def run_matching(refs_path, bibtex_path, output_dir, top_k=5, labels_path=None):
    """Run baseline matching and write predictions parquet.

    Returns a summary dict with counts and MRR@5 (if labels provided).
    """
    refs_rows = load_references(refs_path)
    bibtex_rows = load_bibtex(bibtex_path)
    pubs = join_publications(refs_rows, bibtex_rows)

    rows = []
    per_pub_mrr = {}
    labels = {}
    if labels_path and os.path.exists(labels_path):
        with open(labels_path, encoding="utf-8") as f:
            labels = json.load(f)

    for aid, (bib_list, ref_list) in pubs.items():
        if not bib_list or not ref_list:
            continue
        cands = build_candidates(bib_list, ref_list)
        gt_for_pub = labels.get(aid, {})
        preds_ids = []
        gts = []
        for be, scored in cands:
            key = be.get("citation_key")
            top_refs = [r for r, _ in scored[:top_k]]
            for rank, (ref, score) in enumerate(scored[:top_k], start=1):
                rows.append({
                    "arxiv_id": aid,
                    "citation_key": key,
                    "rank": rank,
                    "ref_arxiv_id": ref.get("ref_arxiv_id"),
                    "score": float(score),
                    "ref_title": ref.get("ref_title"),
                })
            preds_ids.append([r.get("ref_arxiv_id") for r in top_refs])
            # ground truth if labeled: map citation_key -> ref_arxiv_id
            gt = gt_for_pub.get(key) if isinstance(gt_for_pub, dict) else None
            if gt is not None:
                gts.append(gt)
        if gts and len(gts) == len(preds_ids):
            per_pub_mrr[aid] = mrr_at_k(preds_ids, gts, k=top_k)

    os.makedirs(output_dir, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=pa.schema([
        ("arxiv_id", pa.string()),
        ("citation_key", pa.string()),
        ("rank", pa.int64()),
        ("ref_arxiv_id", pa.string()),
        ("score", pa.float64()),
        ("ref_title", pa.string()),
    ]))
    out_path = os.path.join(output_dir, "predictions.parquet")
    pq.write_table(table, out_path)

    mrr_values = list(per_pub_mrr.values())
    summary = {
        "publications": len(pubs),
        "matched_publications": len([p for p in pubs if pubs[p][0] and pubs[p][1]]),
        "prediction_rows": len(rows),
        "labeled_publications": len(per_pub_mrr),
        "mrr_at_k": (sum(mrr_values) / len(mrr_values)) if mrr_values else None,
    }
    with open(os.path.join(output_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary
