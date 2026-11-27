"""CLI for offline M3 reference matching."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.m3.pipeline import run_matching


def main():
    ap = argparse.ArgumentParser(description="Offline M3 reference matcher")
    ap.add_argument("--references", required=True, help="Path to M1 references parquet or dir")
    ap.add_argument("--bibtex", required=True, help="Path to M2 bibtex parquet or dir")
    ap.add_argument("--output", required=True, help="Output directory")
    ap.add_argument("--labels", default=None, help="Optional JSON labels {arxiv_id: {citation_key: ref_arxiv_id}}")
    ap.add_argument("--split", default=None, help="Split JSON from scripts/split_m3.py")
    ap.add_argument("--abstain", type=float, default=None,
                    help="Drop candidates below this score instead of naming them")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()
    summary = run_matching(args.references, args.bibtex, args.output, top_k=args.top_k,
                           labels_path=args.labels, split_path=args.split,
                           abstain=args.abstain)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
