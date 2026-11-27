"""Merge the members' exported labelling files into one label set and check the quota.

Each member exports from the HTML page; the exports are keyed by (arxiv_id, citation_key)
and merged newest-first, so re-exporting after a correction wins. The merged file is the
only thing downstream code reads, and every row carries the reviewer who signed it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.m3.review import validate_labels

ROW_FIELDS = ("arxiv_id", "citation_key", "groundtruth_arxiv_id", "decision", "via",
              "reviewer", "reviewed_at")


def load_export(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("rows", [])
    reviewer = None if isinstance(data, list) else data.get("reviewer")
    return rows, reviewer


def main():
    ap = argparse.ArgumentParser(description="Merge M3 manual label exports")
    ap.add_argument("labels", nargs="+", help="Exported labels JSON files")
    ap.add_argument("--out", help="Merged JSONL to write")
    ap.add_argument("--members", type=int, default=4, help="Group size k (quota check)")
    ap.add_argument("--allow-partial", action="store_true",
                    help="Write the merge even when the quota check fails")
    args = ap.parse_args()

    merged = {}
    for path in args.labels:
        rows, reviewer = load_export(path)
        for row in rows:
            row = dict(row)
            if reviewer and not row.get("reviewer"):
                row["reviewer"] = reviewer
            key = (row.get("arxiv_id"), row.get("citation_key"))
            previous = merged.get(key)
            if previous and (previous.get("reviewed_at") or "") > (row.get("reviewed_at") or ""):
                continue
            merged[key] = row

    rows = [{field: merged[key].get(field) for field in ROW_FIELDS} for key in sorted(merged)]
    report = validate_labels(rows, expected_publications=5 * args.members,
                             minimum_entries=100 * args.members)

    for problem in report["problems"]:
        print(f"  ! {problem}")
    print(f"  labels {report['labels']} ({report['matched']} matched, "
          f"{report['none_rows']} none) over {report['publications']} publications")
    print(f"  reviewers {report['reviewers']}")
    print(f"  decided   {report['via']}")
    print(f"  quota {'met' if report['ok'] else 'NOT met'} for k={args.members}")

    if args.out and (report["ok"] or args.allow_partial):
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                               encoding="utf-8")
        print(f"  wrote {destination}")
    elif args.out:
        print("  not written; fix the problems above or pass --allow-partial")
    sys.exit(0 if report["ok"] or args.allow_partial else 1)


if __name__ == "__main__":
    main()
