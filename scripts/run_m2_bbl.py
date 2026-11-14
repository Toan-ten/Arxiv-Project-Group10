"""Convert M1 source_bbl archives into verified BibTeX and M2 evidence."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.m2.bbl_pipeline import run_bbl_pipeline
from src.m2.latex import ParseError


def main(argv: list[str] | None = None) -> int:
    """Run the single offline BBL conversion pipeline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True,
                        help="Archive or extracted directory; repeat for multiple inputs")
    parser.add_argument("--inventory", help="CSV with archive,path,sha256 for source verification")
    parser.add_argument("--output", required=True, help="Output directory outside the input corpus")
    args = parser.parse_args(argv)
    try:
        report = run_bbl_pipeline([Path(path) for path in args.input], Path(args.output),
                                  inventory=Path(args.inventory) if args.inventory else None)
    except (ParseError, OSError, ValueError) as exc:
        print(f"M2 BBL stopped: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"run_root": report["run_root"], "input_files": report["input_files"],
                      "counts": report["counts"], "wall_seconds": report["wall_seconds"]},
                     ensure_ascii=False, indent=2))
    complete = (report["counts"].get("parse_error", 0) == 0
                and report["counts"].get("publications_requires_review", 0) == 0)
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
