"""Write M3's predictions table for the publications in the train/valid/test split."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.m3 import model
from src.m3.pipeline import PARTITIONS, build_predictions, load_labels, validate_predictions, write_predictions


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


def main():
    ap = argparse.ArgumentParser(description="Build M3 predictions in the statement schema")
    ap.add_argument("--bibtex", required=True)
    ap.add_argument("--references", required=True)
    ap.add_argument("--split", required=True, help="Split JSON from scripts/split_m3.py")
    ap.add_argument("--labels", help="Merged manual labels JSONL (supplies groundtruth)")
    ap.add_argument("--auto-labels", help="Automatic pool labels JSONL (supplies groundtruth)")
    ap.add_argument("--scorer", choices=("heuristic", "logistic", "boosting"), default="heuristic")
    ap.add_argument("--model", help="Fitted model from scripts/train_m3.py (joblib)")
    ap.add_argument("--abstain", type=float, default=None,
                    help="Drop candidates below this probability; the row becomes all-null")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--out", required=True, help="Output directory for the Parquet shards")
    ap.add_argument("--report", help="Write the validation report as JSON")
    args = ap.parse_args()

    bibtex = model.index_by(read_rows(args.bibtex, ("arxiv_id", "citation_key")), "citation_key")
    references = model.group(read_rows(args.references, ("arxiv_id", "ref_arxiv_id")))
    split = json.loads(Path(args.split).read_text(encoding="utf-8"))
    partitions = {}
    for partition in PARTITIONS:
        for pool in ("manual", "auto"):
            for arxiv_id in split.get(partition, {}).get(pool, []):
                partitions[arxiv_id] = partition

    labels = load_labels(args.labels)
    labels.update(load_labels(args.auto_labels))

    scorer = None
    if args.model:
        import joblib

        scorer = joblib.load(args.model)
    elif args.scorer != "heuristic":
        ap.error("--model is required for a trained scorer")

    rankings = {}
    for arxiv_id in sorted(partitions):
        entries = list(bibtex.get(arxiv_id, {}).values())
        refs = references.get(arxiv_id, [])
        if not entries or not refs:
            continue
        matrix, meta = model.cells_for_entries(entries, refs, truths=labels)
        if scorer is None:
            scores = model.HeuristicBaseline().decision_function(matrix)
        else:
            scores = model.probability(scorer, matrix)
        rankings.update(model.rank_entries(meta, scores))

    rows = build_predictions(rankings, partitions, labels, abstain=args.abstain, k=args.k)
    report = validate_predictions(rows, partitions=partitions, references=references)
    written = write_predictions(rows, args.out)

    for partition in PARTITIONS:
        print(f"  {partition:<5} {report['per_partition'].get(partition, 0):>7} rows")
    print(f"  labelled {report['labelled']} | with candidates {report['with_candidates']} | "
          f"abstained {report['abstained']}")
    for problem in report["problems"][:10]:
        print(f"  ! {problem}")
    print(f"  {'valid' if report['ok'] else 'INVALID'} predictions for "
          f"{report['publications']} publications")
    print(f"  wrote {written}")
    if args.report:
        Path(args.report).write_text(json.dumps(dict(report, written=written), indent=2),
                                     encoding="utf-8")
        print(f"  report {args.report}")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
