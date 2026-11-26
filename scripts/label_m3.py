"""CLI for manual M3 labeling and MRR evaluation."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.m3.labeling import evaluate_mrr, interactive_label


def main():
    ap = argparse.ArgumentParser(description="M3 labeling and evaluation")
    ap.add_argument("--references", help="Path to references parquet or dir")
    ap.add_argument("--bibtex", help="Path to bibtex parquet or dir")
    ap.add_argument("--labels", required=True, help="Labels JSON path")
    ap.add_argument("--predictions", help="Predictions parquet path (for --evaluate)")
    ap.add_argument("--limit", type=int, default=5, help="Max publications to label")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--evaluate", action="store_true", help="Evaluate MRR@k instead of labeling")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    if args.evaluate:
        if not args.predictions:
            ap.error("--predictions required with --evaluate")
        res = evaluate_mrr(args.labels, args.predictions, k=args.k)
        print(res)
        return

    if not args.references or not args.bibtex:
        ap.error("--references and --bibtex required for labeling")
    interactive_label(args.references, args.bibtex, args.labels, limit_pubs=args.limit, top_k=args.top_k)


if __name__ == "__main__":
    main()
