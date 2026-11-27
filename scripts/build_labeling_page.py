"""Build the single-file labelling page for M3's manual pool.

Reads the M2 BibTeX entries and the M1 references, keeps the publications the shortlist
chose, and writes one offline HTML file holding every publication, its candidate
references and a draft suggestion per entry.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.m3.matrix import select_labeling_publications
from src.m3.review import build_payload, render_page

TEMPLATE = Path(__file__).resolve().parent.parent / "src" / "m3" / "labelling_page.html"


def read_rows(path, required):
    """Rows carrying ``required`` columns, from a Parquet file or a whole output tree.

    A run directory holds every table the milestone wrote (papers, elements, hierarchy,
    bibtex, ...), so the directory form is filtered down to the table we asked for.
    """
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
    ap = argparse.ArgumentParser(description="Build the M3 manual labelling page")
    ap.add_argument("--bibtex", required=True, help="M2 bibtex Parquet file or directory")
    ap.add_argument("--references", required=True, help="M1 references Parquet file or directory")
    ap.add_argument("--shortlist", help="Shortlist JSON; derived from --members when omitted")
    ap.add_argument("--members", type=int, default=4, help="Group size k (statement quota)")
    ap.add_argument("--names", help="Comma-separated reviewer names, one per member")
    ap.add_argument("--minimum-entries", type=int, default=20,
                    help="Per-publication entry floor (statement floor is 20)")
    ap.add_argument("--maximum-entries", type=int, default=30)
    ap.add_argument("--minimum-references", type=int, default=8)
    ap.add_argument("--out", required=True, help="HTML file to write")
    ap.add_argument("--payload-out", help="Also write the page payload as JSON")
    args = ap.parse_args()

    bibtex = read_rows(args.bibtex, ("arxiv_id", "citation_key"))
    references = read_rows(args.references, ("arxiv_id", "ref_arxiv_id"))
    if not bibtex or not references:
        ap.error("no rows found; --bibtex needs a bibtex table and --references a references table")
    if args.shortlist:
        shortlist = json.loads(Path(args.shortlist).read_text(encoding="utf-8"))
    else:
        wanted = 5 * args.members
        shortlist, _ = select_labeling_publications(
            bibtex, references, minimum_entries=args.minimum_entries,
            maximum_entries=args.maximum_entries, minimum_references=args.minimum_references,
            limit=wanted)
        if len(shortlist) < wanted:
            ap.error(f"only {len(shortlist)} publications qualify, {wanted} needed for "
                     f"{args.members} members; lower --minimum-references")

    names = [name.strip() for name in args.names.split(",")] if args.names else None
    payload = build_payload(bibtex, references, shortlist, members=args.members, names=names)

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_page(payload, TEMPLATE), encoding="utf-8")
    if args.payload_out:
        Path(args.payload_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                          encoding="utf-8")

    summary = payload["summary"]
    print(f"publications : {summary['publications']}")
    print(f"entries      : {summary['entries']} ({summary['labelable_entries']} labelable)")
    print(f"references   : {summary['references']}  cells: {summary['cells']}")
    print(f"draft tiers  : {summary['tiers']}")
    for member, count in summary["per_member_entries"].items():
        print(f"  {member:<10} {count} labels")
    print(f"page         : {destination} ({destination.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
