"""Train M3's ranker and report MRR@5 per partition and per labelling pool."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.m3 import model
from src.m3.split import partition_map


def read_rows(path, required):
    import pyarrow.parquet as pq

    target = Path(path)
    files = sorted(target.rglob("*.parquet")) if target.is_dir() else [target]
    rows = []
    for file in files:
        for row in pq.read_table(file).to_pylist():
            if all(column in row for column in required):
                rows.append(row)
    return rows


def top_title_ratio(ranked, cell_block, threshold):
    """Best title sequence ratio per entry, so entries can be split into easy and hard."""
    import numpy as np

    matrix, _, meta = cell_block
    position = model.MODEL_FEATURES.index("title_sequence_ratio")
    top = {}
    for (arxiv_id, citation_key, _ref, _truth), row in zip(meta, matrix):
        key = (arxiv_id, citation_key)
        value = float(row[position])
        if value > top.get(key, 0.0):
            top[key] = value
    return top


def read_jsonl(path):
    if not path:
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def main():
    ap = argparse.ArgumentParser(description="Train and evaluate the M3 ranker")
    ap.add_argument("--bibtex", required=True)
    ap.add_argument("--references", required=True)
    ap.add_argument("--labels", required=True, help="Merged manual labels JSONL")
    ap.add_argument("--auto-labels", help="Automatic pool labels JSONL")
    ap.add_argument("--split", required=True, help="Split JSON from scripts/split_m3.py")
    ap.add_argument("--partitions", nargs="+", default=["train", "valid", "test"])
    ap.add_argument("--report", help="Write the evaluation report as JSON")
    ap.add_argument("--model-out", help="Write the fitted boosting model with joblib")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--hard-band", type=float, default=0.97,
                    help="Entries whose best title ratio is below this are reported separately; "
                         "they are the ones a model can actually get wrong")
    args = ap.parse_args()

    bibtex = model.index_by(read_rows(args.bibtex, ("arxiv_id", "citation_key")), "citation_key")
    references = model.group(read_rows(args.references, ("arxiv_id", "ref_arxiv_id")))
    manual = read_jsonl(args.labels)
    auto = read_jsonl(args.auto_labels)
    for row in manual:
        row["pool"] = "manual"
    for row in auto:
        row["pool"] = "auto"
    for row in manual + auto:
        row["partition"] = partition_map(json.loads(Path(args.split).read_text(encoding="utf-8"))).get(row["arxiv_id"])

    labelled = {(row["arxiv_id"], row["citation_key"]): row for row in auto + manual}
    by_partition = {name: [row for row in labelled.values() if row["partition"] == name]
                    for name in ("train", "valid", "test")}
    print("labels per partition (manual / auto):")
    for name, rows in by_partition.items():
        print(f"  {name:<5} {sum(1 for r in rows if r['pool'] == 'manual'):>4} / "
              f"{sum(1 for r in rows if r['pool'] == 'auto'):>6}")

    cells = {}
    for name in ("train", "valid", "test"):
        if not by_partition[name]:
            continue
        matrix, labels, meta = model.build_cells(by_partition[name], bibtex, references)
        cells[name] = (matrix, labels, meta)
        print(f"  {name} cells: {len(labels)} ({int(labels.sum())} positive)")

    if "train" not in cells:
        ap.error("no training rows; pass --auto-labels")

    matrix, labels, meta = cells["train"]
    models = {"heuristic": model.HeuristicBaseline(),
              "logistic": model.train("logistic", matrix, labels),
              "boosting": model.train("boosting", matrix, labels)}

    report = {"features": model.MODEL_FEATURES, "k": args.k, "models": {}}
    pool_of = {(row["arxiv_id"], row["citation_key"]): row["pool"] for row in labelled.values()}

    for name, fitted in models.items():
        rankings = {}
        for partition, (pm, pl, pmeta) in cells.items():
            rankings[partition] = model.rank_entries(pmeta, model.probability(fitted, pm))

        abstention = None
        if "valid" in rankings:
            best, table = model.tune_abstention(rankings["valid"], k=args.k)
            abstention = best["threshold"]
            report.setdefault("abstention", {})[name] = best

        entry = {}
        for partition, ranked in rankings.items():
            if partition == "train":
                continue
            entry[partition] = {}
            for label, subset in (("all", ranked),
                                  ("manual", {k: v for k, v in ranked.items() if pool_of.get(k) == "manual"}),
                                  ("auto", {k: v for k, v in ranked.items() if pool_of.get(k) == "auto"})):
                if not subset:
                    continue
                entry[partition][label] = model.mrr_at_k(subset, k=args.k, abstain=abstention)
        # A slice of entries where the top candidate is not an obvious near-verbatim title.
        # Machine-generated labels make the easy slice trivially perfect for every scorer,
        # so this is the part of the table that can tell two models apart.
        for partition, ranked in rankings.items():
            if partition == "train":
                continue
            top = top_title_ratio(ranked, cells[partition], args.hard_band)
            hard = {key: value for key, value in ranked.items() if top.get(key, 1.0) < args.hard_band}
            if hard:
                entry[partition]["hard"] = model.mrr_at_k(hard, k=args.k, abstain=abstention)
        entry["abstention"] = abstention
        report["models"][name] = entry

    print(f"\nMRR@{args.k} (abstention tuned on validation)\n")
    header = f"{'model':<10} {'partition':<7} {'pool':<7} {'MRR':>7} {'entries':>8} {'hits':>6} {'none':>5} {'abstain':>8}"
    print(header)
    print("-" * len(header))
    for name, entry in report["models"].items():
        for partition in ("valid", "test"):
            for pool in ("all", "manual", "auto", "hard"):
                row = entry.get(partition, {}).get(pool)
                if not row:
                    continue
                print(f"{name:<10} {partition:<7} {pool:<7} {row['mrr']:>7.4f} {row['entries']:>8} "
                      f"{row['hits']:>6} {row['none_entries']:>5} {row['abstained']:>8}")

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nreport: {args.report}")
    if args.model_out:
        import joblib

        joblib.dump(models["boosting"], args.model_out)
        print(f"model : {args.model_out}")


if __name__ == "__main__":
    main()
