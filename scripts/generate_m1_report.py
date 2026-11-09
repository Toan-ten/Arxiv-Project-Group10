"""Generate M1 Milestone 1 execution statistics and markdown report tables.

Computes comprehensive crawling, storage, extraction, metadata, and reference coverage metrics
strictly aligned with the M1 Storage Contract (src.storage, src.storage_integrity, src.reporting)
for insertion into the group's Jupyter Book technical report.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from src.id_ranges import iter_assigned_ids
from src.reporting import (
    METRIC_DEFINITION_VERSION,
    REPORT_SCHEMA_VERSION,
    _get_git_commit,
    compute_m1_statistics,
    export_report_json,
    format_markdown_summary,
)
from src.storage import CorpusStorage

__all__ = [
    "METRIC_DEFINITION_VERSION",
    "REPORT_SCHEMA_VERSION",
    "_get_git_commit",
    "compute_m1_statistics",
    "export_report_json",
    "format_markdown_summary",
]


def main():
    parser = argparse.ArgumentParser(description="Generate Milestone 1 summary statistics report.")
    parser.add_argument("--corpus", required=True, help="Path to corpus root directory")
    parser.add_argument("--output", default="m1_report_summary.md", help="Output Markdown report path")
    parser.add_argument("--json", default=None, help="Optional output JSON report path")
    args = parser.parse_args()

    store = CorpusStorage(Path(args.corpus))
    stats = compute_m1_statistics(store, iter_assigned_ids())
    md = format_markdown_summary(stats)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"Đã xuất báo cáo thống kê M1 ra tệp: {out_path}")

    if args.json:
        json_path = Path(args.json)
        export_report_json(stats, json_path)
        print(f"Đã xuất dữ liệu số liệu M1 ra tệp JSON: {json_path}")

    print(md)


if __name__ == "__main__":
    main()
