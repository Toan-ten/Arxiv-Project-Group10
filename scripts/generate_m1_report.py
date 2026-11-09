"""Generate M1 Milestone 1 execution statistics and markdown report tables.

Computes comprehensive crawling, storage, extraction, and reference coverage metrics
strictly aligned with the M1 Storage Contract (src.storage) for insertion into
the group's Jupyter Book technical report.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.storage import (
    CorpusStorage,
    IntegrityError,
    SOURCE_ERRORS,
    StorageError,
    TERMINAL,
    arxiv_id_to_folder_key,
)

METRIC_DEFINITION_VERSION = "2.0.0"


def _get_git_commit(cwd: Path) -> str:
    """Attempt to get current git commit SHA, falling back to UNKNOWN."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "UNKNOWN"


def compute_m1_statistics(
    store: CorpusStorage,
    pairs: Iterable[Tuple[str, str]],
    *,
    repository_commit: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute all canonical Milestone 1 crawling, extraction, and reference metrics.

    Adheres strictly to the M1 v2 Storage Contract:
    - Version terminal states: {"success", "no_tex_source"} (no "complete" state).
    - Source complete: found, resolved versions, all versions terminal, sources verified.
    - M1 complete: source complete, metadata complete, references complete.
    - References complete: status="complete" AND coverage.complete is True.
    - Robust error accounting: tracks missing, invalid, and unprocessed records.
    """
    pairs_list = list(pairs)
    total_candidates = len(pairs_list)

    # Paper level counters
    found_papers = 0
    not_found_papers = 0
    unprocessed_papers = 0
    missing_manifests = 0
    invalid_manifests = 0
    source_complete_papers = 0
    m1_complete_papers = 0
    partial_papers = 0
    failed_papers = 0
    interrupted_papers = 0

    # Version level counters
    total_versions = 0
    successful_tex_versions = 0
    no_tex_versions = 0
    terminal_versions = 0
    incomplete_versions = 0
    failed_versions = 0

    # Reference counters
    complete_reference_papers = 0
    partial_reference_papers = 0
    failed_reference_papers = 0
    not_attempted_reference_papers = 0
    invalid_references = 0
    total_complete_reference_records = 0
    total_observed_reference_records = 0

    for mssv, arxiv_id in pairs_list:
        folder_key = arxiv_id_to_folder_key(arxiv_id)
        manifest_file = store.root / folder_key / "manifest.json"

        if not manifest_file.exists():
            missing_manifests += 1
            continue

        try:
            manifest = store.read_manifest(mssv, arxiv_id)
        except (StorageError, IntegrityError, Exception):
            invalid_manifests += 1
            continue

        disc = manifest.get("discovery_status")
        if disc == "not_found":
            not_found_papers += 1
            continue
        elif disc == "not_attempted":
            unprocessed_papers += 1
            continue
        elif disc == "found":
            found_papers += 1
            versions = manifest.get("versions") or {}
            total_versions += len(versions)

            # Check source completion
            is_source_complete = False
            if versions and manifest.get("version_status") == "resolved":
                if all(v.get("status") in TERMINAL for v in versions.values()):
                    is_source_complete = True
            if is_source_complete:
                source_complete_papers += 1

            # Count versions by terminal/incomplete/failed classification
            for v_name, v_info in versions.items():
                st = v_info.get("status")
                if st == "success":
                    successful_tex_versions += 1
                    terminal_versions += 1
                elif st == "no_tex_source":
                    no_tex_versions += 1
                    terminal_versions += 1
                elif st in {"pending", "in_progress", "interrupted"}:
                    incomplete_versions += 1
                elif st in SOURCE_ERRORS:
                    failed_versions += 1

            # Check paper stage classification
            paper_status = manifest.get("status")
            if paper_status in {"complete", "complete_with_no_tex"}:
                m1_complete_papers += 1
            elif paper_status == "partial":
                partial_papers += 1
            elif paper_status == "failed":
                failed_papers += 1
            elif paper_status == "interrupted":
                interrupted_papers += 1

            # References verification
            ref_file = store.root / folder_key / "references.json"
            if ref_file.exists():
                try:
                    ref_data = store.read_references(mssv, arxiv_id)
                    ref_st = ref_data.get("status")
                    coverage = ref_data.get("coverage") or {}
                    cov_complete = coverage.get("complete") is True
                    items = ref_data.get("items") or []
                    item_count = len(items)
                    total_observed_reference_records += item_count

                    if ref_st == "complete" and cov_complete:
                        complete_reference_papers += 1
                        total_complete_reference_records += item_count
                    elif ref_st == "partial" or (ref_st == "complete" and not cov_complete):
                        partial_reference_papers += 1
                    elif ref_st == "failed":
                        failed_reference_papers += 1
                    elif ref_st == "not_attempted":
                        not_attempted_reference_papers += 1
                except Exception:
                    invalid_references += 1
            else:
                not_attempted_reference_papers += 1
        else:
            failed_papers += 1

    # Computed percentages and averages
    source_completion_rate = (
        round(source_complete_papers / found_papers * 100.0, 2) if found_papers > 0 else 0.0
    )
    m1_completion_rate = (
        round(m1_complete_papers / found_papers * 100.0, 2) if found_papers > 0 else 0.0
    )
    version_extraction_success_rate = (
        round(successful_tex_versions / total_versions * 100.0, 2) if total_versions > 0 else 0.0
    )
    no_tex_rate = (
        round(no_tex_versions / total_versions * 100.0, 2) if total_versions > 0 else 0.0
    )
    references_coverage_rate = (
        round(complete_reference_papers / found_papers * 100.0, 2) if found_papers > 0 else 0.0
    )
    average_references_per_paper = (
        round(total_complete_reference_records / complete_reference_papers, 2)
        if complete_reference_papers > 0
        else 0.0
    )

    commit_sha = repository_commit or _get_git_commit(store.root)

    return {
        "provenance": {
            "report_generated_at": datetime.now(timezone.utc).isoformat(),
            "repository_commit": commit_sha,
            "corpus_root": str(store.root.resolve()),
            "metric_definition_version": METRIC_DEFINITION_VERSION,
        },
        "total_candidates": total_candidates,
        "found_papers": found_papers,
        "not_found_papers": not_found_papers,
        "unprocessed_papers": unprocessed_papers,
        "missing_manifests": missing_manifests,
        "invalid_manifests": invalid_manifests,
        "invalid_references": invalid_references,
        "source_complete_papers": source_complete_papers,
        "m1_complete_papers": m1_complete_papers,
        "partial_papers": partial_papers,
        "failed_papers": failed_papers,
        "interrupted_papers": interrupted_papers,
        "total_versions": total_versions,
        "successful_tex_versions": successful_tex_versions,
        "no_tex_versions": no_tex_versions,
        "terminal_versions": terminal_versions,
        "incomplete_versions": incomplete_versions,
        "failed_versions": failed_versions,
        "complete_reference_papers": complete_reference_papers,
        "partial_reference_papers": partial_reference_papers,
        "failed_reference_papers": failed_reference_papers,
        "not_attempted_reference_papers": not_attempted_reference_papers,
        "total_complete_reference_records": total_complete_reference_records,
        "total_observed_reference_records": total_observed_reference_records,
        "source_completion_rate": source_completion_rate,
        "m1_completion_rate": m1_completion_rate,
        "version_extraction_success_rate": version_extraction_success_rate,
        "no_tex_rate": no_tex_rate,
        "references_coverage_rate": references_coverage_rate,
        "average_references_per_paper": average_references_per_paper,
        # Backward compatibility aliases
        "completed_papers": source_complete_papers,
        "completed_versions": terminal_versions,
        "success_rate": source_completion_rate,
        "total_references": total_complete_reference_records,
        "papers_with_references": complete_reference_papers,
    }


def format_markdown_summary(stats: Dict[str, Any]) -> str:
    """Format computed statistics into a standard Markdown report for Jupyter Book."""
    prov = stats.get("provenance", {})
    lines = [
        "### Báo cáo thống kê thu thập dữ liệu Milestone 1",
        "",
        "#### 1. Xuất xứ kiểm toán (Audit Provenance)",
        "",
        "| Thuộc tính | Giá trị |",
        "|---|---|",
        f"| Thời điểm tạo báo cáo | `{prov.get('report_generated_at', 'N/A')}` |",
        f"| Mã băm mã nguồn (Commit) | `{prov.get('repository_commit', 'N/A')}` |",
        f"| Thư mục lưu trữ Corpus | `{prov.get('corpus_root', 'N/A')}` |",
        f"| Phiên bản định nghĩa số liệu | `v{prov.get('metric_definition_version', 'N/A')}` |",
        "",
        "#### 2. Chỉ số thu thập và chất lượng dữ liệu (Canonical Metrics)",
        "",
        "| Chỉ số kiểm toán M1 | Số lượng | Tỷ lệ (%) / Ghi chú |",
        "|---|---:|---:|",
        f"| Tổng số định danh phân công | {stats['total_candidates']:,} | 100.0% |",
        f"| Bài báo tồn tại trên arXiv (found) | {stats['found_papers']:,} | Ứng viên thực tế |",
        f"| Bài báo không tồn tại (not_found) | {stats['not_found_papers']:,} | Định danh trống |",
        f"| Bài báo hoàn tất mã nguồn (source_complete) | {stats['source_complete_papers']:,} | {stats['source_completion_rate']}% bài tìm thấy |",
        f"| Bài báo hoàn tất toàn bộ Milestone 1 (m1_complete) | {stats['m1_complete_papers']:,} | {stats['m1_completion_rate']}% bài tìm thấy |",
        f"| Bài báo thu thập dở dang (partial) | {stats['partial_papers']:,} | Cần resume |",
        f"| Tổng số phiên bản bài báo (versions) | {stats['total_versions']:,} | Toàn bộ phiên bản |",
        f"| Phiên bản trích xuất TeX thành công (success) | {stats['successful_tex_versions']:,} | {stats['version_extraction_success_rate']}% tổng version |",
        f"| Phiên bản không chứa TeX hợp lệ (no_tex_source) | {stats['no_tex_versions']:,} | {stats['no_tex_rate']}% tổng version |",
        f"| Bài báo có trích dẫn hoàn tất (complete_refs) | {stats['complete_reference_papers']:,} | {stats['references_coverage_rate']}% bài tìm thấy |",
        f"| Tổng số liên kết trích dẫn hoàn tất | {stats['total_complete_reference_records']:,} | TB: {stats['average_references_per_paper']} trích dẫn / bài |",
        f"| Hồ sơ lỗi / thiếu (missing / invalid manifests) | {stats['missing_manifests'] + stats['invalid_manifests']:,} | Cần kiểm toán |",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Generate Milestone 1 summary statistics report.")
    parser.add_argument("--corpus", required=True, help="Path to corpus root directory")
    parser.add_argument("--output", default="m1_report_summary.md", help="Output Markdown report path")
    args = parser.parse_args()

    from src.id_ranges import iter_assigned_ids
    store = CorpusStorage(Path(args.corpus))
    stats = compute_m1_statistics(store, iter_assigned_ids())
    md = format_markdown_summary(stats)

    out_path = Path(args.output)
    out_path.write_text(md, encoding="utf-8")
    print(f"Đã xuất báo cáo thống kê M1 ra tệp: {out_path}")
    print(md)


if __name__ == "__main__":
    main()
