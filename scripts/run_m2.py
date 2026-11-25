"""CLI for the offline M2 parser."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.m2.latex import ParseError
from src.m2.runner import run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--paper", help="Exact base arXiv ID")
    parser.add_argument("--version", help="Exact version, e.g. v2")
    parser.add_argument("--limit-versions", type=int)
    parser.add_argument("--selection", help="JSON list of {arxiv_id, version} for a reproducible sample")
    args = parser.parse_args(argv)
    try:
        selected = json.loads(Path(args.selection).read_text(encoding="utf-8-sig")) if args.selection else None
        result = run(args.corpus, args.output, paper=args.paper, version=args.version,
                     limit_versions=args.limit_versions, selected_versions=selected)
    except (ParseError, OSError, ValueError, KeyError) as exc:
        print(f"M2 stopped: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: v for k, v in result.items() if k != "versions_detail"}, ensure_ascii=False, indent=2))
    # Nonzero means some versions require review, even though good outputs were preserved.
    return 0 if result["all_versions_processed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
