"""Fill M3's automatic pool with the classic matcher.

Runs the heuristic over every publication the manual pool did not take, keeps the ones
whose labels actually teach something, and writes the label rows the model will train on.
With ``--calibrate`` it also sweeps the acceptance thresholds against a human-labelled set
and reports which pair to ship.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.m3 import auto_match


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
    ap = argparse.ArgumentParser(description="Build M3's automatic matching pool")
    ap.add_argument("--bibtex", required=True)
    ap.add_argument("--references", required=True)
    ap.add_argument("--manual-shortlist", help="Manual pool JSON to exclude")
    ap.add_argument("--members", type=int, default=4, help="Group size k")
    ap.add_argument("--limit", type=int, help="Auto publications to keep (default 10%% of the rest, floor 2k)")
    ap.add_argument("--min-matches", type=int, default=auto_match.MIN_MATCHES_PER_PUBLICATION)
    ap.add_argument("--max-entries", type=int, default=auto_match.MAX_ENTRIES_PER_PUBLICATION,
                    help="Skip survey-sized publications; 0 disables the cap")
    ap.add_argument("--seed", type=int, default=auto_match.POOL_SEED)
    ap.add_argument("--threshold", type=float, default=auto_match.THRESHOLD)
    ap.add_argument("--margin", type=float, default=auto_match.MARGIN)
    ap.add_argument("--out-labels", required=True, help="JSONL of auto label rows")
    ap.add_argument("--out-publications", required=True, help="JSON of the chosen publications")
    ap.add_argument("--calibrate", help="Human labels JSONL to sweep the thresholds against")
    ap.add_argument("--precision-floor", type=float, default=0.90)
    ap.add_argument("--jobs-limit", type=int, default=0, help="Only score this many publications (debug)")
    args = ap.parse_args()

    bibtex = read_rows(args.bibtex, ("arxiv_id", "citation_key"))
    references = read_rows(args.references, ("arxiv_id", "ref_arxiv_id"))
    manual = set()
    if args.manual_shortlist:
        manual = {row["arxiv_id"] for row in
                  json.loads(Path(args.manual_shortlist).read_text(encoding="utf-8"))}

    if args.calibrate:
        rows = [json.loads(line) for line in Path(args.calibrate).read_text(encoding="utf-8").splitlines() if line.strip()]
        curve = auto_match.calibrate(rows, bibtex, references)
        best = auto_match.best_setting(curve, minimum_precision=args.precision_floor)
        print(f"calibration over {len(rows)} human-labelled rows")
        print(f"{'thr':>5} {'margin':>7} {'prec':>6} {'recall':>7} {'f1':>6} {'accepted':>9} {'false+':>7}")
        for row in sorted(curve, key=lambda r: -r["f1"])[:8]:
            print(f"{row['threshold']:>5.2f} {row['margin']:>7.2f} {row['precision']:>6.3f} "
                  f"{row['recall']:>7.3f} {row['f1']:>6.3f} {row['accepted']:>9} {row['false_positive']:>7}")
        print(f"best at precision >= {args.precision_floor}: threshold {best['threshold']:.2f}, "
              f"margin {best['margin']:.2f} (precision {best['precision']:.3f}, recall {best['recall']:.3f})")
        return

    pool = sorted((set(row["arxiv_id"] for row in bibtex) &
                   set(row["arxiv_id"] for row in references)) - manual)
    if args.jobs_limit:
        pool = pool[:args.jobs_limit]
    limit = args.limit or max(2 * args.members, -(-len(pool) * 10 // 100))
    print(f"scoring {len(pool)} publications (manual pool excluded: {len(manual)}) ...")

    started = time.time()
    rows, summary = auto_match.label_corpus(bibtex, references, pool, threshold=args.threshold,
                                            margin_floor=args.margin)
    print(f"  scored in {time.time() - started:.0f}s")
    stats = auto_match.pool_statistics(rows, summary)
    print(f"  entries {stats['entries']}, matched {stats['matched']} ({stats['match_rate']:.1%}), "
          f"methods {stats['methods']}")
    print(f"  publications with any match: {stats['publications_with_matches']}/{stats['publications']}")

    selected, eligible = auto_match.select_auto_publications(
        summary, limit, minimum_matches=args.min_matches, exclude=manual,
        maximum_entries=args.max_entries or None, seed=args.seed)
    print(f"  eligible (>= {args.min_matches} matches, <= {args.max_entries or 'no'} entries): "
          f"{len(eligible)}; keeping {len(selected)} of {limit} wanted "
          f"(10% of the rest = {-(-len(pool) * 10 // 100)}, floor 2k = {2 * args.members})")
    if len(selected) < limit:
        print(f"  ! only {len(selected)} publications qualify; lower --min-matches")

    keep = {row["arxiv_id"] for row in selected}
    kept_rows = [row for row in rows if row["arxiv_id"] in keep]
    Path(args.out_labels).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_labels).write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept_rows), encoding="utf-8")
    Path(args.out_publications).write_text(json.dumps(
        dict(threshold=args.threshold, margin=args.margin, min_matches=args.min_matches,
             candidates=len(pool), seed=args.seed, eligible=len(eligible),
             selected=selected), indent=2), encoding="utf-8")

    matched = sum(1 for row in kept_rows if row["decision"] == auto_match.MATCHED)
    print(f"  wrote {len(kept_rows)} label rows ({matched} matched, "
          f"{len(kept_rows) - matched} none) over {len(selected)} publications")
    print(f"  labels {args.out_labels}")
    print(f"  pool   {args.out_publications}")


if __name__ == "__main__":
    main()
