"""Generate M1 Milestone 1 execution statistics and markdown report tables.

Computes comprehensive crawling, storage, extraction, metadata, and reference coverage metrics
strictly aligned with the M1 Storage Contract (src.storage, src.storage_integrity, src.reporting)
for insertion into the group's Jupyter Book technical report.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.bbl_handling import validate_bbl_source_policy
from src.id_ranges import iter_assigned_ids
from src.reporting import (
    METRIC_DEFINITION_VERSION,
    REPORT_SCHEMA_VERSION,
    _get_git_commit,
    compute_m1_statistics,
    export_report_json,
    format_markdown_summary,
)
from src.storage import CorpusStorage, TERMINAL

__all__ = [
    "METRIC_DEFINITION_VERSION",
    "REPORT_SCHEMA_VERSION",
    "_get_git_commit",
    "compute_m1_statistics",
    "export_report_json",
    "format_markdown_summary",
]


def _validate_bbl_artifacts(store: CorpusStorage, pairs) -> None:
    """Fail closed if a corpus policy references missing or invalid BBL artifacts."""
    for mssv, arxiv_id in pairs:
        try:
            manifest = store.read_manifest(mssv, arxiv_id)
        except (OSError, ValueError, KeyError, TypeError):
            continue  # Báo cáo chuẩn phân loại lỗi của manifest.
        versions = manifest.get("versions")
        if not isinstance(versions, dict):
            continue  # Báo cáo chuẩn phân loại bảng phiên bản sai định dạng.
        for version, record in versions.items():
            if not isinstance(record, dict) or record.get("status") not in TERMINAL:
                continue
            policy = record.get("bbl_policy")
            if policy is None:
                continue  # Điểm khôi phục cũ không có bản ghi chính sách BBL ngoài corpus.
            validate_bbl_source_policy(
                store._path(mssv, arxiv_id, version),
                record.get("files", []), policy,
                artifact_root=store.bbl_artifact_root,
                mssv=mssv, arxiv_id=arxiv_id, version=version,
            )


def main():
    parser = argparse.ArgumentParser(description="Generate Milestone 1 summary statistics report.")
    parser.add_argument("--corpus", required=True, help="Path to corpus root directory")
    parser.add_argument(
        "--bbl-artifact-root", default=None,
        help=("Path to the external BBL policy artifacts, usually "
              "<drive-root>/reports/derived_bibliography/bbl-exception-v1"),
    )
    parser.add_argument("--output", default="m1_report_summary.md", help="Output Markdown report path")
    parser.add_argument("--json", default=None, help="Optional output JSON report path")
    args = parser.parse_args()

    store = CorpusStorage(
        Path(args.corpus), bbl_artifact_root=args.bbl_artifact_root,
    )
    pairs = list(iter_assigned_ids())
    try:
        _validate_bbl_artifacts(store, pairs)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        parser.error(
            "Không thể xác minh artifact .bbl; kiểm tra --bbl-artifact-root "
            f"và checksum artifact ({type(exc).__name__}: {exc})."
        )
    stats = compute_m1_statistics(store, pairs)
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
