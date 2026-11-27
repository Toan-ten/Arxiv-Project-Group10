"""Write M3's train/validation/test split and check it against the statement's quota."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.m3.split import assign_partitions, validate_split


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser(description="Split M3 publications into train/valid/test")
    ap.add_argument("--manual-shortlist", required=True, help="Manual pool JSON")
    ap.add_argument("--auto-publications", required=True, help="Automatic pool JSON")
    ap.add_argument("--members", type=int, default=4, help="Group size k")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--labels", help="Merged labels JSONL, to check label counts per partition")
    ap.add_argument("--out", required=True, help="Split JSON to write")
    args = ap.parse_args()

    manual = [row["arxiv_id"] for row in read_json(args.manual_shortlist)]
    auto = [row["arxiv_id"] for row in read_json(args.auto_publications)["selected"]]
    split = assign_partitions(manual, auto, args.members, seed=args.seed)

    labels = None
    if args.labels:
        labels = [json.loads(line) for line in Path(args.labels).read_text(encoding="utf-8").splitlines()
                  if line.strip()]

    report = validate_split(split, args.members, labels)
    for partition in ("train", "valid", "test"):
        counts = report["counts"][partition]
        print(f"  {partition:<5} manual {counts['manual']:>3}  auto {counts['auto']:>3}")
    if args.labels:
        print(f"  labels per partition: {report['labels']['per_partition']}")
    for problem in report["problems"]:
        print(f"  ! {problem}")
    print(f"  split {'valid' if report['ok'] else 'INVALID'} for k={args.members} (seed {args.seed})")

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(dict(split, report=report), indent=2), encoding="utf-8")
    print(f"  wrote {destination}")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
