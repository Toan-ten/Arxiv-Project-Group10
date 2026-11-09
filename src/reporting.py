"""Canonical Reporting and Statistics Module for Milestone 1.

Computes comprehensive crawling, storage, extraction, metadata, reference coverage,
and M1 milestone completeness metrics strictly aligned with the M1 v2 Storage Contract
(docs/m1_contract.md, src.storage, src.storage_integrity, and docs/reporting_metrics.md).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.id_ranges import ConfigError, arxiv_id_to_folder_key
from src.storage import (
    CONTRACT_VERSION,
    CorpusStorage,
    IntegrityError,
    SOURCE_ERRORS,
    StorageError,
    TERMINAL,
)
from src.storage_integrity import audit_paper_integrity, audit_version_integrity

METRIC_DEFINITION_VERSION = "2.1.0"
REPORT_SCHEMA_VERSION = "2.1.0"


def _get_git_commit(cwd: Optional[Path] = None) -> str:
    """Attempt to get current 40-character Git commit SHA, falling back to UNKNOWN.

    Searches cwd if provided, then PROJECT_ROOT, then repository root of this script.
    """
    candidates: List[Path] = []
    if cwd is not None:
        candidates.append(Path(cwd))

    try:
        from src.local_config import PROJECT_ROOT
        candidates.append(PROJECT_ROOT)
    except Exception:
        pass

    try:
        candidates.append(Path(__file__).resolve().parents[1])
    except Exception:
        pass

    for target_dir in candidates:
        try:
            sha = subprocess.check_output(
                ["git", "-C", str(target_dir), "rev-parse", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                return sha
        except Exception:
            continue
    return "UNKNOWN"


def compute_m1_statistics(
    store: CorpusStorage,
    pairs: Iterable[Tuple[str, str]],
    *,
    repository_commit: Optional[str] = None,
    report_scope: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute all canonical Milestone 1 crawling, extraction, metadata, and reference metrics.

    Adheres strictly to the M1 v2 Storage Contract and Canonical Metric Specification:
    - Evidence-first verification: integrates audit_paper_integrity and audit_version_integrity.
    - Mutually exclusive & exhaustive candidate classification.
    - Preserves null semantics (None/N/A for zero denominators, no artificial 0.0%).
    - Enforces M1 completion invariant: source_complete and meta_complete and refs_complete.
    """
    pairs_list = list(pairs)
    total_candidates = len(pairs_list)

    # 1. Primary Paper Classification (Mutually Exclusive Partition of assigned_candidates)
    found_papers = 0
    confirmed_not_found = 0
    unprocessed_papers = 0
    missing_manifests = 0
    invalid_manifests = 0
    discovery_failed = 0
    other_unresolved = 0

    # 2. Stage Completion Counters
    source_complete_papers = 0
    metadata_complete_papers = 0
    references_complete_papers = 0
    m1_complete_papers = 0

    # 3. Found Papers Lifecycle Counters (Mutually Exclusive Partition of found_papers)
    partial_papers = 0
    failed_papers = 0
    interrupted_papers = 0
    unresolved_found_papers = 0
    integrity_failed_papers = 0

    # 4. Version Level Counters
    total_discovered_versions = 0
    successful_tex_versions = 0
    no_tex_versions = 0
    terminal_versions = 0
    incomplete_versions = 0
    failed_versions = 0
    invalid_versions = 0

    # 5. Reference Counters
    complete_reference_papers = 0
    partial_reference_papers = 0
    failed_reference_papers = 0
    not_attempted_reference_papers = 0
    invalid_reference_papers = 0
    total_complete_reference_records = 0
    total_observed_reference_records = 0

    for mssv, arxiv_id in pairs_list:
        try:
            folder_key = arxiv_id_to_folder_key(arxiv_id)
        except (ConfigError, Exception):
            other_unresolved += 1
            continue

        paper_dir = store.root / folder_key

        # Check paper directory presence and path safety
        if not paper_dir.exists():
            missing_manifests += 1
            continue

        if paper_dir.is_symlink() or os.path.islink(paper_dir):
            other_unresolved += 1
            integrity_failed_papers += 1
            continue

        manifest_path = paper_dir / "manifest.json"
        if not manifest_path.exists():
            missing_manifests += 1
            continue

        if manifest_path.is_symlink() or os.path.islink(manifest_path):
            invalid_manifests += 1
            integrity_failed_papers += 1
            continue

        try:
            manifest = store.read_manifest(mssv, arxiv_id)
        except (StorageError, IntegrityError, json.JSONDecodeError, OSError, Exception):
            invalid_manifests += 1
            integrity_failed_papers += 1
            continue

        disc = manifest.get("discovery_status")

        if disc == "not_found":
            # Verify negative discovery contract (R14: versions must be exact empty dict)
            versions = manifest.get("versions")
            if (
                manifest.get("status") == "not_found"
                and isinstance(versions, dict)
                and not versions
            ):
                confirmed_not_found += 1
            else:
                invalid_manifests += 1
                integrity_failed_papers += 1
            continue

        elif disc == "not_attempted":
            unprocessed_papers += 1
            continue

        elif disc == "failed":
            discovery_failed += 1
            continue

        elif disc == "found":
            found_papers += 1
            versions = manifest.get("versions")
            if not isinstance(versions, dict):
                versions = {}

            total_discovered_versions += len(versions)

            # Audit paper integrity using canonical Phase 1 API
            audit_res = audit_paper_integrity(store, mssv, arxiv_id)
            if not audit_res.is_valid:
                integrity_failed_papers += 1

            # --- A. Source Completion Check ---
            if audit_res.is_source_complete:
                source_complete_papers += 1

            # --- B. Metadata Completion Check ---
            # Verify metadata file independently
            is_meta_complete = False
            meta_path = paper_dir / "metadata.json"
            if meta_path.exists() and not meta_path.is_symlink() and not os.path.islink(meta_path):
                try:
                    meta = store.read_metadata(mssv, arxiv_id)
                    if (
                        meta.get("status") == "complete"
                        and manifest.get("metadata_status") == "complete"
                        and not any(
                            v.severity == "error"
                            and ("metadata" in v.message.lower() or (v.file_path and "metadata" in v.file_path))
                            for v in audit_res.violations
                        )
                    ):
                        is_meta_complete = True
                except Exception:
                    pass

            if is_meta_complete:
                metadata_complete_papers += 1

            # --- C. References Completion Check ---
            is_ref_complete = False
            ref_path = paper_dir / "references.json"
            if ref_path.exists() and not ref_path.is_symlink() and not os.path.islink(ref_path):
                try:
                    ref_data = store.read_references(mssv, arxiv_id)
                    ref_st = ref_data.get("status")
                    coverage = ref_data.get("coverage") or {}
                    cov_complete = coverage.get("complete") is True and coverage.get("continuation") is None
                    items = ref_data.get("items") or []
                    item_count = len(items) if isinstance(items, list) else 0
                    total_observed_reference_records += item_count

                    if (
                        ref_st == "complete"
                        and cov_complete
                        and manifest.get("references_status") == "complete"
                        and not any(
                            v.severity == "error"
                            and ("reference" in v.message.lower() or (v.file_path and "references" in v.file_path))
                            for v in audit_res.violations
                        )
                    ):
                        is_ref_complete = True
                        complete_reference_papers += 1
                        total_complete_reference_records += item_count
                    elif ref_st == "partial" or (ref_st == "complete" and not cov_complete):
                        partial_reference_papers += 1
                    elif ref_st == "failed":
                        failed_reference_papers += 1
                    elif ref_st == "not_attempted":
                        not_attempted_reference_papers += 1
                    else:
                        invalid_reference_papers += 1
                except Exception:
                    invalid_reference_papers += 1
            else:
                not_attempted_reference_papers += 1

            if is_ref_complete:
                references_complete_papers += 1

            # --- D. Milestone 1 Complete Invariant ---
            # Strict: source complete and metadata complete and references complete and no violations
            if audit_res.is_m1_complete and is_meta_complete and is_ref_complete:
                m1_complete_papers += 1

            # --- E. Found Papers Lifecycle Classification (Mutually Exclusive) ---
            man_status = manifest.get("status")
            if audit_res.is_m1_complete and is_meta_complete and is_ref_complete:
                pass  # Accounted as m1_complete
            elif man_status == "partial":
                partial_papers += 1
            elif man_status == "failed":
                failed_papers += 1
            elif man_status == "interrupted":
                interrupted_papers += 1
            else:
                unresolved_found_papers += 1

            # --- F. Version-Level Accounting ---
            for v_name, v_info in versions.items():
                if not isinstance(v_info, dict):
                    invalid_versions += 1
                    continue
                v_st = v_info.get("status")
                if v_st == "success":
                    successful_tex_versions += 1
                    terminal_versions += 1
                elif v_st == "no_tex_source":
                    no_tex_versions += 1
                    terminal_versions += 1
                elif v_st in {"pending", "in_progress", "interrupted"}:
                    incomplete_versions += 1
                elif v_st in SOURCE_ERRORS:
                    failed_versions += 1
                else:
                    invalid_versions += 1

        else:
            other_unresolved += 1

    # Computed percentages (None when denominator == 0, explicit semantics)
    source_completion_rate: Optional[float] = (
        round(source_complete_papers / found_papers * 100.0, 2) if found_papers > 0 else None
    )
    metadata_completion_rate: Optional[float] = (
        round(metadata_complete_papers / found_papers * 100.0, 2) if found_papers > 0 else None
    )
    references_coverage_rate: Optional[float] = (
        round(complete_reference_papers / found_papers * 100.0, 2) if found_papers > 0 else None
    )
    m1_completion_rate: Optional[float] = (
        round(m1_complete_papers / found_papers * 100.0, 2) if found_papers > 0 else None
    )
    version_extraction_success_rate: Optional[float] = (
        round(successful_tex_versions / total_discovered_versions * 100.0, 2)
        if total_discovered_versions > 0
        else None
    )
    no_tex_rate: Optional[float] = (
        round(no_tex_versions / total_discovered_versions * 100.0, 2)
        if total_discovered_versions > 0
        else None
    )
    average_references_per_paper: Optional[float] = (
        round(total_complete_reference_records / complete_reference_papers, 2)
        if complete_reference_papers > 0
        else None
    )

    # Reconciliation Checks
    paper_population_balance = (
        found_papers
        + confirmed_not_found
        + unprocessed_papers
        + discovery_failed
        + missing_manifests
        + invalid_manifests
        + other_unresolved
    )
    found_lifecycle_balance = (
        m1_complete_papers
        + partial_papers
        + failed_papers
        + interrupted_papers
        + unresolved_found_papers
    )
    version_balance = (
        terminal_versions
        + incomplete_versions
        + failed_versions
        + invalid_versions
    )

    is_reconciled = (
        paper_population_balance == total_candidates
        and found_lifecycle_balance == found_papers
        and version_balance == total_discovered_versions
        and m1_complete_papers <= source_complete_papers
        and m1_complete_papers <= metadata_complete_papers
        and m1_complete_papers <= references_complete_papers
    )

    commit_sha = repository_commit or _get_git_commit(store.root)

    try:
        corpus_root_str = str(store.root.resolve())
    except (OSError, RuntimeError):
        corpus_root_str = str(store.root)

    return {
        "provenance": {
            "report_generated_at": datetime.now(timezone.utc).isoformat(),
            "repository_commit": commit_sha,
            "metric_definition_version": METRIC_DEFINITION_VERSION,
            "storage_contract_version": CONTRACT_VERSION,
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "corpus_root": corpus_root_str,
            "assigned_candidate_count": total_candidates,
            "report_scope": report_scope or "cohort_assignment",
        },
        # Candidate Accounting
        "assigned_candidates": total_candidates,
        "total_candidates": total_candidates,  # Compatibility alias
        "found_papers": found_papers,
        "confirmed_not_found": confirmed_not_found,
        "not_found_papers": confirmed_not_found,  # Compatibility alias
        "unprocessed_papers": unprocessed_papers,
        "missing_manifests": missing_manifests,
        "invalid_manifests": invalid_manifests,
        "discovery_failed": discovery_failed,
        "other_unresolved": other_unresolved,
        # Stage & M1 Completion
        "source_complete_papers": source_complete_papers,
        "metadata_complete_papers": metadata_complete_papers,
        "references_complete_papers": references_complete_papers,
        "m1_complete_papers": m1_complete_papers,
        # Found Lifecycle Partition
        "partial_papers": partial_papers,
        "failed_papers": failed_papers,
        "interrupted_papers": interrupted_papers,
        "unresolved_found_papers": unresolved_found_papers,
        "integrity_failed_papers": integrity_failed_papers,
        # Version Accounting
        "total_discovered_versions": total_discovered_versions,
        "total_versions": total_discovered_versions,  # Compatibility alias
        "successful_tex_versions": successful_tex_versions,
        "no_tex_versions": no_tex_versions,
        "terminal_versions": terminal_versions,
        "incomplete_versions": incomplete_versions,
        "failed_versions": failed_versions,
        "invalid_versions": invalid_versions,
        # References Accounting
        "complete_reference_papers": complete_reference_papers,
        "partial_reference_papers": partial_reference_papers,
        "failed_reference_papers": failed_reference_papers,
        "not_attempted_reference_papers": not_attempted_reference_papers,
        "invalid_reference_papers": invalid_reference_papers,
        "invalid_references": invalid_reference_papers,  # Compatibility alias
        "total_complete_reference_records": total_complete_reference_records,
        "total_observed_reference_records": total_observed_reference_records,
        # Rates and Averages
        "source_completion_rate": source_completion_rate,
        "metadata_completion_rate": metadata_completion_rate,
        "references_coverage_rate": references_coverage_rate,
        "m1_completion_rate": m1_completion_rate,
        "version_extraction_success_rate": version_extraction_success_rate,
        "no_tex_rate": no_tex_rate,
        "average_references_per_paper": average_references_per_paper,
        # Reconciliation Results
        "is_reconciled": is_reconciled,
        "reconciliation": {
            "paper_population_balance": paper_population_balance,
            "paper_population_expected": total_candidates,
            "found_lifecycle_balance": found_lifecycle_balance,
            "found_lifecycle_expected": found_papers,
            "version_balance": version_balance,
            "version_expected": total_discovered_versions,
            "m1_hierarchy_valid": (
                m1_complete_papers <= source_complete_papers
                and m1_complete_papers <= metadata_complete_papers
                and m1_complete_papers <= references_complete_papers
            ),
        },
        # Backward compatibility aliases
        "completed_papers": source_complete_papers,
        "completed_versions": terminal_versions,
        "success_rate": source_completion_rate if source_completion_rate is not None else 0.0,
        "total_references": total_complete_reference_records,
        "papers_with_references": complete_reference_papers,
    }


def _fmt_rate(val: Optional[float], suffix: str = "%") -> str:
    """Format nullable percentage/rate for Markdown tables."""
    if val is None:
        return "N/A"
    return f"{val}{suffix}"


def format_markdown_summary(stats: Dict[str, Any]) -> str:
    """Format computed statistics into standard, verified Markdown for technical reports."""
    prov = stats.get("provenance", {})
    reconciled_badge = "ĐẠT (Balanced)" if stats.get("is_reconciled") else "CẦN KIỂM TRA (Unbalanced)"

    lines = [
        "### Báo cáo kiểm toán và thống kê thu thập dữ liệu Milestone 1",
        "",
        "#### 1. Xuất xứ kiểm toán (Audit Provenance)",
        "",
        "| Thuộc tính | Giá trị |",
        "|---|---|",
        f"| Thời điểm tạo báo cáo | `{prov.get('report_generated_at', 'N/A')}` |",
        f"| Mã băm mã nguồn (Commit) | `{prov.get('repository_commit', 'N/A')}` |",
        f"| Thư mục lưu trữ Corpus | `{prov.get('corpus_root', 'N/A')}` |",
        f"| Phiên bản định nghĩa số liệu | `v{prov.get('metric_definition_version', 'N/A')}` |",
        f"| Phiên bản hợp đồng lưu trữ | `v{prov.get('storage_contract_version', 'N/A')}` |",
        f"| Trạng thái đối chiếu số liệu | **{reconciled_badge}** |",
        "",
        "#### 2. Chỉ số tiến độ các giai đoạn & Chất lượng M1 (Canonical Stage Metrics)",
        "",
        "| Chỉ số kiểm toán M1 | Số lượng | Tỷ lệ (%) / Mẫu số | Ghi chú |",
        "|---|---:|---:|---|",
        f"| Tổng định danh phân công (assigned_candidates) | {stats['assigned_candidates']:,} | 100.0% | Dải ID cấu hình |",
        f"| Bài báo tồn tại trên arXiv (found_papers) | {stats['found_papers']:,} | {_fmt_rate(stats['found_papers'] / stats['assigned_candidates'] * 100 if stats['assigned_candidates'] > 0 else None)} | Ứng viên thực tế |",
        f"| Bài báo không tồn tại (confirmed_not_found) | {stats['confirmed_not_found']:,} | {_fmt_rate(stats['confirmed_not_found'] / stats['assigned_candidates'] * 100 if stats['assigned_candidates'] > 0 else None)} | Xác nhận âm tính |",
        f"| Bài báo hoàn tất mã nguồn (source_complete) | {stats['source_complete_papers']:,} | {_fmt_rate(stats['source_completion_rate'])} | Trên bài tìm thấy (found) |",
        f"| Bài báo hoàn tất siêu dữ liệu (metadata_complete) | {stats['metadata_complete_papers']:,} | {_fmt_rate(stats['metadata_completion_rate'])} | Trên bài tìm thấy (found) |",
        f"| Bài báo hoàn tất trích dẫn (references_complete) | {stats['complete_reference_papers']:,} | {_fmt_rate(stats['references_coverage_rate'])} | Trên bài tìm thấy (found) |",
        f"| Bài báo hoàn tất toàn diện M1 (m1_complete) | {stats['m1_complete_papers']:,} | {_fmt_rate(stats['m1_completion_rate'])} | Đạt 100% tiêu chí M1 |",
        f"| Bài báo thu thập dở dang (partial_papers) | {stats['partial_papers']:,} | Cần resume | Giai đoạn chưa hoàn tất |",
        f"| Bài báo thất bại (failed_papers) | {stats['failed_papers']:,} | Cần xử lý lỗi | Gặp lỗi terminal |",
        f"| Bài báo bị gián đoạn (interrupted_papers) | {stats['interrupted_papers']:,} | Cần resume | Do timeout / cancel |",
        f"| Bài báo có vi phạm tính toàn vẹn (integrity_failed) | {stats['integrity_failed_papers']:,} | Cần kiểm toán | Sai SHA/tệp rác |",
        "",
        "#### 3. Thống kê bóc tách phiên bản (Version-Level Statistics)",
        "",
        "| Phân loại phiên bản | Số lượng | Tỷ lệ (%) / Mẫu số | Ghi chú |",
        "|---|---:|---:|---|",
        f"| Tổng phiên bản quan sát được (total_discovered_versions) | {stats['total_discovered_versions']:,} | 100.0% | Từ bài tìm thấy |",
        f"| Phiên bản trích xuất TeX thành công (successful_tex) | {stats['successful_tex_versions']:,} | {_fmt_rate(stats['version_extraction_success_rate'])} | Có tệp .tex xác minh |",
        f"| Phiên bản không chứa TeX hợp lệ (no_tex_source) | {stats['no_tex_versions']:,} | {_fmt_rate(stats['no_tex_rate'])} | Terminal hợp lệ |",
        f"| Tổng phiên bản kết thúc (terminal_versions) | {stats['terminal_versions']:,} | {_fmt_rate(stats['terminal_versions'] / stats['total_discovered_versions'] * 100 if stats['total_discovered_versions'] > 0 else None)} | success + no_tex |",
        f"| Phiên bản chưa xong (incomplete_versions) | {stats['incomplete_versions']:,} | {_fmt_rate(stats['incomplete_versions'] / stats['total_discovered_versions'] * 100 if stats['total_discovered_versions'] > 0 else None)} | pending / in_progress |",
        f"| Phiên bản bóc tách lỗi (failed_versions) | {stats['failed_versions']:,} | {_fmt_rate(stats['failed_versions'] / stats['total_discovered_versions'] * 100 if stats['total_discovered_versions'] > 0 else None)} | Lỗi nguồn arXiv |",
        "",
        "#### 4. Thống kê quan hệ trích dẫn (References Statistics)",
        "",
        "| Chỉ số trích dẫn | Giá trị | Ghi chú |",
        "|---|---:|---|",
        f"| Bài báo trích dẫn hoàn tất (complete_refs) | {stats['complete_reference_papers']:,} | Đạt coverage.complete |",
        f"| Bài báo trích dẫn dở dang (partial_refs) | {stats['partial_reference_papers']:,} | Cần bổ sung trích dẫn |",
        f"| Bài báo trích dẫn lỗi (failed_refs) | {stats['failed_reference_papers']:,} | Lỗi API S2 |",
        f"| Bài báo chưa thu thập trích dẫn (not_attempted) | {stats['not_attempted_reference_papers']:,} | Chưa cào |",
        f"| Tổng liên kết trích dẫn hoàn tất | {stats['total_complete_reference_records']:,} | Thu thập từ bài complete |",
        f"| Số liên kết trích dẫn trung bình / bài | {stats['average_references_per_paper'] if stats['average_references_per_paper'] is not None else 'N/A'} | Trên bài complete |",
        "",
        "#### 5. Bảng kiểm tra đối chiếu số liệu (Reconciliation Summary)",
        "",
        "| Hạng mục đối chiếu | Giá trị tính toán | Kỳ vọng | Kết luận |",
        "|---|---:|---:|:---:|",
        f"| Cân bằng tập định danh phân công | {stats['reconciliation']['paper_population_balance']:,} | {stats['assigned_candidates']:,} | {'KHỚP' if stats['reconciliation']['paper_population_balance'] == stats['assigned_candidates'] else 'LỆCH'} |",
        f"| Cân bằng vòng đời bài tìm thấy | {stats['reconciliation']['found_lifecycle_balance']:,} | {stats['found_papers']:,} | {'KHỚP' if stats['reconciliation']['found_lifecycle_balance'] == stats['found_papers'] else 'LỆCH'} |",
        f"| Cân bằng phân loại phiên bản | {stats['reconciliation']['version_balance']:,} | {stats['total_discovered_versions']:,} | {'KHỚP' if stats['reconciliation']['version_balance'] == stats['total_discovered_versions'] else 'LỆCH'} |",
        f"| Thứ bậc M1 complete (M1 <= Source, Meta, Refs) | {stats['m1_complete_papers']:,} <= {min(stats['source_complete_papers'], stats['metadata_complete_papers'], stats['references_complete_papers']):,} | Hợp lệ | {'HỢP LỆ' if stats['reconciliation']['m1_hierarchy_valid'] else 'VI PHẠM'} |",
    ]
    return "\n".join(lines) + "\n"


def export_report_json(stats: Dict[str, Any], out_path: Path) -> None:
    """Save canonical statistics dictionary as structured, indented JSON."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
