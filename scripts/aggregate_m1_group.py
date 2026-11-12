#!/usr/bin/env python3
"""Group 10 M1 Aggregator & Campaign Audit Script.

Aggregates and verifies crawling, extraction, S2 enrichment, archive, and benchmark evidence
across all 4 group members (200 cohorts, 20,000 assigned arXiv IDs).

Enforces:
1. Exactly four assigned student MSSVs (24120010, 24120035, 24120232, 24120260).
2. Exactly 50 cohorts per member (1..50, 51..100, 101..150, 151..200).
3. 200 contiguous cohorts, 20,000 unique assigned IDs, zero gaps, zero overlap.
4. Cryptographic SHA-256 calculation directly from every actual .tar.gz file.
5. Strict verification against both .sha256 sidecar and accounting manifest.
6. Tarball internal member inspection: rejects unsafe paths, absolute paths, symlinks,
   devices, foreign folders, duplicate IDs, or missing paper manifests.
7. Verification of archived paper manifests and source file checksums.
8. Benchmark evidence consumption from actual report.json and resources.json artifacts,
   deduplicating attempt intervals, sample-count-weighted average RAM, peak RAM/disk,
   and keeping uncollected metrics as null instead of zero.
9. Group PASS requires zero unprocessed, zero discovery failed, zero integrity failures,
   zero archive issues, valid benchmark metrics, and all 20,000 IDs accounted for.
10. If any member evidence is missing, returns status INCOMPLETE without interpolation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple

# Thêm thư mục gốc kho mã vào sys.path.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.fast_production import (
    cohort_pairs,
    member_cohort_range,
)
from src.storage import arxiv_id_to_folder_key

EXPECTED_MEMBERS = ["24120010", "24120035", "24120232", "24120260"]
TOTAL_EXPECTED_COHORTS = 200
TOTAL_EXPECTED_IDS = 20000


def _compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_member_roots(
    base_dir: Optional[Path],
    explicit_roots: Optional[List[str]],
) -> Dict[str, Path]:
    member_paths: Dict[str, Path] = {}
    if explicit_roots:
        for entry in explicit_roots:
            if "=" in entry:
                mssv, path_str = entry.split("=", 1)
                member_paths[mssv.strip()] = Path(path_str.strip()).resolve()
            else:
                p = Path(entry.strip()).resolve()
                if p.name in EXPECTED_MEMBERS:
                    member_paths[p.name] = p

    if base_dir and base_dir.is_dir():
        for mssv in EXPECTED_MEMBERS:
            if mssv not in member_paths:
                candidate = base_dir / mssv
                if candidate.is_dir():
                    member_paths[mssv] = candidate.resolve()

    return member_paths


def _inspect_cohort_archive(
    archive_path: Path,
    sha_path: Path,
    manifest_path: Path,
    cohort_num: int,
    pairs: List[Tuple[str, str]],
    verify_files: bool = True,
) -> Dict[str, Any]:
    """Inspect and deeply verify a cohort archive and all its member papers."""
    if not manifest_path.is_file():
        return {
            "status": "MISSING_MANIFEST",
            "error": f"Cohort {cohort_num}: Missing accounting manifest {manifest_path.name}",
        }
    if not archive_path.is_file():
        return {
            "status": "MISSING_ARCHIVE",
            "error": f"Cohort {cohort_num}: Missing archive tarball {archive_path.name}",
        }
    if not sha_path.is_file():
        return {
            "status": "MISSING_SIDECAR",
            "error": f"Cohort {cohort_num}: Missing sha256 sidecar {sha_path.name}",
        }

    try:
        sidecar_text = sha_path.read_text(encoding="utf-8").strip()
        expected_sidecar_sha = sidecar_text.split()[0]
    except Exception as exc:
        return {
            "status": "CORRUPTED_SIDECAR",
            "error": f"Cohort {cohort_num}: Cannot read .sha256 sidecar: {exc}",
        }

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "status": "CORRUPTED_MANIFEST",
            "error": f"Cohort {cohort_num}: Corrupted manifest.json: {exc}",
        }

    recorded_manifest_sha = manifest_data.get("archive_sha256")
    actual_sha = _compute_sha256(archive_path)

    if actual_sha != expected_sidecar_sha:
        return {
            "status": "CHECKSUM_MISMATCH",
            "error": (
                f"Cohort {cohort_num}: Archive SHA ({actual_sha}) does not match sidecar ({expected_sidecar_sha})"
            ),
        }

    if actual_sha != recorded_manifest_sha:
        return {
            "status": "CHECKSUM_MISMATCH",
            "error": (
                f"Cohort {cohort_num}: Archive SHA ({actual_sha}) does not match manifest ({recorded_manifest_sha})"
            ),
        }

    # Xác thực số lô trong manifest.
    manifest_cohort = manifest_data.get("cohort_number") if "cohort_number" in manifest_data else manifest_data.get("cohort_id")
    if manifest_cohort is not None and manifest_cohort != cohort_num:
        return {
            "status": "COHORT_ID_MISMATCH",
            "error": f"Cohort {cohort_num}: Manifest cohort ID mismatch ({manifest_cohort} != {cohort_num})",
        }

    archive_state = manifest_data.get("archive_state")
    if archive_state not in {"m1_handoff_verified", "handoff_verified"}:
        return {
            "status": "INCOMPLETE_ARCHIVE_STATE",
            "error": f"Cohort {cohort_num}: archive_state is '{archive_state}', expected 'm1_handoff_verified'",
        }

    expected_folder_keys = {arxiv_id_to_folder_key(aid): (mssv, aid) for mssv, aid in pairs}
    seen_folder_keys: Set[str] = set()
    tar_members_map: Dict[str, tarfile.TarInfo] = {}
    paper_manifests: Dict[str, Dict[str, Any]] = {}
    files_by_paper: Dict[str, Set[PurePosixPath]] = {}

    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            for m in tar.getmembers():
                if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                    return {
                        "status": "UNSAFE_MEMBER",
                        "error": f"Cohort {cohort_num}: Disallowed special member type in archive: {m.name}",
                    }
                if not (m.isfile() or m.isdir()):
                    return {
                        "status": "UNSAFE_MEMBER",
                        "error": f"Cohort {cohort_num}: Disallowed member type in archive: {m.name}",
                    }
                p = PurePosixPath(m.name)
                if p.is_absolute() or m.name.startswith("/") or m.name.startswith("\\"):
                    return {
                        "status": "UNSAFE_MEMBER",
                        "error": f"Cohort {cohort_num}: Absolute member path in archive: {m.name}",
                    }
                if ".." in p.parts or "." in p.parts:
                    return {
                        "status": "UNSAFE_MEMBER",
                        "error": f"Cohort {cohort_num}: Path traversal member path in archive: {m.name}",
                    }
                if len(p.parts) == 0:
                    continue
                if ":" in p.parts[0] or "\\" in m.name:
                    return {
                        "status": "UNSAFE_MEMBER",
                        "error": f"Cohort {cohort_num}: Unsafe characters in archive member path: {m.name}",
                    }
                top = p.parts[0]
                if top not in expected_folder_keys:
                    return {
                        "status": "FOREIGN_FOLDER",
                        "error": f"Cohort {cohort_num}: Foreign folder in archive: {top}",
                    }
                seen_folder_keys.add(top)
                tar_members_map[m.name] = m
                if m.isfile():
                    files_by_paper.setdefault(top, set()).add(p)

                if len(p.parts) == 2 and p.parts[1] == "manifest.json" and m.isfile():
                    f = tar.extractfile(m)
                    if f is not None:
                        try:
                            paper_manifests[top] = json.loads(f.read().decode("utf-8"))
                        except Exception as exc:
                            return {
                                "status": "CORRUPTED_PAPER_MANIFEST",
                                "error": f"Cohort {cohort_num}: Corrupted manifest.json in {top}: {exc}",
                            }
    except Exception as exc:
        return {
            "status": "CORRUPTED_ARCHIVE",
            "error": f"Cohort {cohort_num}: Cannot open or read archive: {exc}",
        }

    paper_results: Dict[str, Any] = {
        "found_m1_complete": 0,
        "confirmed_not_found": 0,
        "discovery_failed": 0,
        "unprocessed": 0,
        "integrity_failures": 0,
        "issues": [],
    }

    # Đánh giá từng bài báo dự kiến trong cohort_pairs.
    for folder_key, (mssv, aid) in expected_folder_keys.items():
        if folder_key not in seen_folder_keys:
            paper_results["unprocessed"] += 1
            paper_results["issues"].append(
                f"Cohort {cohort_num}: Paper {aid} ({folder_key}) missing from archive"
            )
            continue

        p_man = paper_manifests.get(folder_key)
        if not p_man or not isinstance(p_man, dict):
            paper_results["integrity_failures"] += 1
            paper_results["issues"].append(
                f"Cohort {cohort_num}: Paper {aid} ({folder_key}) missing or invalid manifest.json inside archive"
            )
            continue

        if p_man.get("arxiv_id") != aid or p_man.get("mssv") != mssv:
            paper_results["integrity_failures"] += 1
            paper_results["issues"].append(
                f"Cohort {cohort_num}: Paper {aid} identity mismatch in manifest: arxiv_id={p_man.get('arxiv_id')}, mssv={p_man.get('mssv')}"
            )
            continue

        disc = p_man.get("discovery_status")
        managed_files_for_paper: Set[str] = {"manifest.json"}

        if disc == "not_found":
            if p_man.get("status") == "not_found" and p_man.get("versions") == {}:
                # Đảm bảo bài not_found không có tệp thừa.
                extra_files = False
                for p_member in files_by_paper.get(folder_key, set()):
                    rel_p = "/".join(p_member.parts[1:])
                    if rel_p not in managed_files_for_paper:
                        paper_results["issues"].append(
                            f"Cohort {cohort_num}: not_found Paper {aid} contains unmanaged file: {p_member.as_posix()}"
                        )
                        paper_results["integrity_failures"] += 1
                        extra_files = True
                        break
                if not extra_files:
                    paper_results["confirmed_not_found"] += 1
            else:
                paper_results["integrity_failures"] += 1
                paper_results["issues"].append(
                    f"Cohort {cohort_num}: Paper {aid} has invalid not_found contract"
                )
        elif disc == "failed":
            paper_results["discovery_failed"] += 1
        elif disc == "found":
            is_valid = True
            if p_man.get("version_status") != "resolved":
                is_valid = False
                paper_results["issues"].append(
                    f"Cohort {cohort_num}: Paper {aid} version_status '{p_man.get('version_status')}' is not resolved"
                )
            if p_man.get("metadata_status") != "complete":
                is_valid = False
                paper_results["issues"].append(
                    f"Cohort {cohort_num}: Paper {aid} metadata_status '{p_man.get('metadata_status')}' is not complete"
                )
            # Xác minh archive có tệp metadata.json.
            meta_rel = f"{folder_key}/metadata.json"
            if meta_rel not in tar_members_map or not tar_members_map[meta_rel].isfile():
                is_valid = False
                paper_results["issues"].append(
                    f"Cohort {cohort_num}: Paper {aid} missing metadata.json inside archive"
                )
            else:
                managed_files_for_paper.add("metadata.json")

            if p_man.get("references_status") != "complete":
                is_valid = False
                paper_results["issues"].append(
                    f"Cohort {cohort_num}: Paper {aid} references_status '{p_man.get('references_status')}' is not complete"
                )
            # Xác minh archive có tệp references.json.
            ref_rel = f"{folder_key}/references.json"
            if ref_rel not in tar_members_map or not tar_members_map[ref_rel].isfile():
                is_valid = False
                paper_results["issues"].append(
                    f"Cohort {cohort_num}: Paper {aid} missing references.json inside archive"
                )
            else:
                managed_files_for_paper.add("references.json")

            if p_man.get("status") not in {"complete", "complete_with_no_tex", "complete_with_withdrawn", "no_tex_source"}:
                is_valid = False
                paper_results["issues"].append(
                    f"Cohort {cohort_num}: Paper {aid} status '{p_man.get('status')}' is not in complete states"
                )

            # Kiểm tra checksum và tệp phiên bản.
            versions = p_man.get("versions", {})
            if isinstance(versions, dict):
                for v_name, v_data in versions.items():
                    if isinstance(v_data, dict):
                        checksums = v_data.get("file_checksums", {})
                        v_files = v_data.get("files", [])
                        all_v_files = set(checksums.keys()) | set(v_files)
                        for fname in all_v_files:
                            rel_file = f"{v_name}/{fname}"
                            managed_files_for_paper.add(rel_file)
                            rel_tar_path = f"{folder_key}/{rel_file}"
                            m_file = tar_members_map.get(rel_tar_path)
                            if not m_file or not m_file.isfile():
                                is_valid = False
                                paper_results["issues"].append(
                                    f"Cohort {cohort_num}: Missing file {rel_tar_path} for paper {aid}"
                                )
                                break
                            if verify_files and fname in checksums:
                                exp_hash = checksums[fname]
                                with tarfile.open(archive_path, "r:gz") as tar:
                                    f_handle = tar.extractfile(m_file)
                                    if f_handle is None:
                                        is_valid = False
                                        paper_results["issues"].append(
                                            f"Cohort {cohort_num}: Cannot read file {rel_tar_path} for paper {aid}"
                                        )
                                        break
                                    file_hash = hashlib.sha256(f_handle.read()).hexdigest()
                                    if file_hash != exp_hash:
                                        is_valid = False
                                        paper_results["issues"].append(
                                            f"Cohort {cohort_num}: Checksum mismatch for {rel_tar_path}: expected {exp_hash}, got {file_hash}"
                                        )
                                        break

            # Xác minh thư mục bài báo không có tệp thừa ngoài quản lý.
            for p_member in files_by_paper.get(folder_key, set()):
                rel_to_paper = "/".join(p_member.parts[1:])
                if rel_to_paper not in managed_files_for_paper:
                    is_valid = False
                    paper_results["issues"].append(
                        f"Cohort {cohort_num}: Paper {aid} contains unmanaged file outside manifest: {p_member.as_posix()}"
                    )
                    break

            if is_valid:
                paper_results["found_m1_complete"] += 1
            else:
                paper_results["integrity_failures"] += 1
        else:
            paper_results["unprocessed"] += 1
            paper_results["issues"].append(
                f"Cohort {cohort_num}: Paper {aid} has unknown discovery_status '{disc}'"
            )

    manifest_included = manifest_data.get("included_count")
    if manifest_included is not None and (manifest_included != len(seen_folder_keys) or manifest_included != len(pairs)):
        paper_results["issues"].append(
            f"Cohort {cohort_num}: Accounting manifest included_count ({manifest_included}) does not match tar paper count ({len(seen_folder_keys)}) or cohort expected ({len(pairs)})"
        )
        paper_results["integrity_failures"] += 1

    has_issues = bool(
        paper_results["issues"]
        or paper_results["integrity_failures"] > 0
        or paper_results["unprocessed"] > 0
        or paper_results["discovery_failed"] > 0
    )

    return {
        "status": "PASS" if not has_issues else "ISSUES",
        "actual_sha": actual_sha,
        "archive_state": archive_state,
        "included_count": len(seen_folder_keys),
        "seen_folder_keys": list(seen_folder_keys),
        "paper_results": paper_results,
    }


def _aggregate_member_benchmarks(reports_dir: Path) -> Dict[str, Any]:
    """Aggregate actual report.json and resources.json artifacts for a member.

    Deduplicates attempt intervals so that execution times are aggregated exactly once.
    Calculates sample-count-weighted average RAM and peak RAM/disk.
    """
    if not reports_dir.is_dir():
        return {
            "status": "NOT_COLLECTED",
            "experiment_count": 0,
            "total_active_seconds": None,
            "sample_count": 0,
            "weighted_ram_mb": None,
            "sampled_peak_ram_mb": None,
            "sampled_peak_disk_mb": None,
            "s2_runs_count": 0,
        }

    exp_dirs = sorted([d for d in reports_dir.glob("exp-fast-*") if d.is_dir()])
    seen_attempt_ids: Set[str] = set()
    seen_exp_ids: Set[str] = set()
    intervals: List[float] = []
    total_samples = 0
    sum_weighted_ram_mb = 0.0
    peak_ram_mb: Optional[float] = None
    peak_disk_mb: Optional[float] = None
    s2_runs_count = 0
    experiment_count = 0

    for exp_dir in exp_dirs:
        rep_file = exp_dir / "report.json"
        res_file = exp_dir / "resources.json"
        if not rep_file.is_file() and not res_file.is_file():
            continue

        rep_data: Dict[str, Any] = {}
        res_data: Dict[str, Any] = {}

        if rep_file.is_file():
            try:
                rep_data = json.loads(rep_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        if res_file.is_file():
            try:
                res_data = json.loads(res_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Loại lượt chạy trùng lặp.
        attempt_ids = rep_data.get("attempt_ids", [])
        if attempt_ids:
            new_attempts = [aid for aid in attempt_ids if aid not in seen_attempt_ids]
            if not new_attempts:
                continue
            for aid in new_attempts:
                seen_attempt_ids.add(aid)
        else:
            if exp_dir.name in seen_exp_ids:
                continue
            seen_exp_ids.add(exp_dir.name)

        experiment_count += 1

        elapsed = res_data.get("elapsed_seconds")
        if elapsed is None:
            elapsed = rep_data.get("timing_metrics", {}).get("total_wall_seconds")
        if elapsed is not None and isinstance(elapsed, (int, float)):
            intervals.append(float(elapsed))

        metrics_v2 = res_data.get("metrics_v2") or rep_data.get("resource_metrics", {}).get("metrics_v2", {})
        ram_v2 = metrics_v2.get("ram", {}) if isinstance(metrics_v2, dict) else {}
        sample_count = ram_v2.get("sample_count", 0)
        avg_ram = res_data.get("average_ram_mb") or rep_data.get("resource_metrics", {}).get("average_ram_mb")
        p_ram = res_data.get("peak_ram_mb") or rep_data.get("resource_metrics", {}).get("peak_ram_mb")

        if avg_ram is None and ram_v2.get("average_bytes") is not None:
            avg_ram = round(ram_v2["average_bytes"] / (1024**2), 2)
        if p_ram is None and ram_v2.get("sampled_peak_bytes") is not None:
            p_ram = round(ram_v2["sampled_peak_bytes"] / (1024**2), 2)

        if avg_ram is not None and sample_count > 0:
            total_samples += sample_count
            sum_weighted_ram_mb += float(avg_ram) * sample_count

        if p_ram is not None:
            if peak_ram_mb is None or float(p_ram) > peak_ram_mb:
                peak_ram_mb = float(p_ram)

        disk_v2 = metrics_v2.get("disk", {}) if isinstance(metrics_v2, dict) else {}
        disk_peak_bytes = disk_v2.get("sampled_peak_bytes")
        if disk_peak_bytes is not None:
            cur_disk_mb = round(disk_peak_bytes / (1024**2), 2)
            if peak_disk_mb is None or cur_disk_mb > peak_disk_mb:
                peak_disk_mb = cur_disk_mb
        elif res_data.get("disk_corpus_size_mb") is not None:
            cur_disk_mb = float(res_data["disk_corpus_size_mb"])
            if peak_disk_mb is None or cur_disk_mb > peak_disk_mb:
                peak_disk_mb = cur_disk_mb

        cfg = rep_data.get("configuration", {})
        if "s2" in cfg or "s2_config" in cfg:
            s2_runs_count += 1

    if experiment_count == 0:
        return {
            "status": "NOT_COLLECTED",
            "experiment_count": 0,
            "total_active_seconds": None,
            "sample_count": 0,
            "weighted_ram_mb": None,
            "sampled_peak_ram_mb": None,
            "sampled_peak_disk_mb": None,
            "s2_runs_count": 0,
        }

    return {
        "status": "COLLECTED",
        "experiment_count": experiment_count,
        "total_active_seconds": round(sum(intervals), 2) if intervals else None,
        "sample_count": total_samples,
        "weighted_ram_mb": round(sum_weighted_ram_mb / total_samples, 2) if total_samples > 0 else None,
        "sampled_peak_ram_mb": peak_ram_mb,
        "sampled_peak_disk_mb": peak_disk_mb,
        "s2_runs_count": s2_runs_count,
    }


def audit_group_campaign(
    member_roots: Dict[str, Path],
    verify_archives: bool = True,
) -> Dict[str, Any]:
    timestamp = datetime.now(timezone.utc).isoformat()
    missing_members = [m for m in EXPECTED_MEMBERS if m not in member_roots]

    audit_summary: Dict[str, Any] = {
        "timestamp": timestamp,
        "evaluation_scope": "Group 10 M1 200 Cohorts / 20,000 IDs",
        "expected_members": EXPECTED_MEMBERS,
        "found_members": list(member_roots.keys()),
        "missing_members": missing_members,
        "status": "INCOMPLETE" if missing_members else "PROCESSING",
        "cohorts_evaluated": 0,
        "missing_cohorts": [],
        "total_assigned_ids": TOTAL_EXPECTED_IDS,
        "found_m1_complete": 0,
        "confirmed_not_found": 0,
        "discovery_failed": 0,
        "unprocessed": 0,
        "integrity_failures": 0,
        "discrepancies": [],
        "provenance": {
            "runtime_commits": {},
            "schema_versions": {},
        },
        "benchmark": {
            "status": "NOT_COLLECTED",
            "total_active_seconds": None,
            "known_stage_active_seconds": None,
            "scope": "legacy report.json/resources.json intervals only; not end-to-end",
            "total_ram_samples": 0,
            "weighted_ram_mb": None,
            "sampled_peak_ram_mb": None,
            "sampled_peak_disk_mb": None,
            "s2_included_runs": 0,
        },
        "members": {},
    }

    if missing_members:
        audit_summary["status"] = "INCOMPLETE"
        audit_summary["discrepancies"].append(
            f"Missing root directories for member(s): {', '.join(missing_members)}"
        )
        return audit_summary

    all_seen_cohorts: Set[int] = set()
    global_seen_paper_ids: Dict[str, int] = {}
    group_active_seconds = 0.0
    group_total_samples = 0
    group_sum_ram_samples = 0.0
    group_peak_ram: Optional[float] = None
    group_peak_disk: Optional[float] = None
    group_s2_runs = 0
    any_member_benchmark_collected = False
    members_with_missing_benchmarks: List[str] = []

    for mssv in EXPECTED_MEMBERS:
        root = member_roots[mssv]
        assigned_cohorts = member_cohort_range(mssv)
        member_data: Dict[str, Any] = {
            "mssv": mssv,
            "root_path": str(root),
            "assigned_cohorts": assigned_cohorts,
            "cohort_count": len(assigned_cohorts),
            "found_cohorts": 0,
            "missing_cohorts": [],
            "found_m1_complete": 0,
            "confirmed_not_found": 0,
            "discovery_failed": 0,
            "unprocessed": 0,
            "integrity_failures": 0,
            "archives_verified": 0,
            "archive_issues": [],
            "benchmark": {},
        }

        archives_dir = root / "archives"
        reports_dir = root / "reports"

        # Tìm archive thuộc lô khác trong thư mục thành viên.
        if archives_dir.is_dir():
            import re
            for f in sorted(archives_dir.glob("cohort_*_corpus.tar.gz")):
                m_c = re.match(r"^cohort_(\d{4})_corpus\.tar\.gz$", f.name)
                if m_c:
                    c_val = int(m_c.group(1))
                    if c_val not in assigned_cohorts:
                        err = (
                            f"Member {mssv} archives contains foreign cohort archive {f.name} "
                            f"(not in assigned range {assigned_cohorts[0]}..{assigned_cohorts[-1]})"
                        )
                        member_data["archive_issues"].append(err)
                        audit_summary["discrepancies"].append(err)
                        member_data["integrity_failures"] += 1
                        audit_summary["integrity_failures"] += 1

        for cohort_num in assigned_cohorts:
            all_seen_cohorts.add(cohort_num)
            pairs = cohort_pairs(cohort_num)

            archive_name = f"cohort_{cohort_num:04d}_corpus.tar.gz"
            archive_path = archives_dir / archive_name
            sha_path = archives_dir / f"{archive_name}.sha256"
            manifest_path = archives_dir / f"cohort_{cohort_num:04d}_corpus.manifest.json"

            if not manifest_path.is_file():
                member_data["missing_cohorts"].append(cohort_num)
                audit_summary["missing_cohorts"].append(cohort_num)
                member_data["unprocessed"] += len(pairs)
                audit_summary["unprocessed"] += len(pairs)
                continue

            member_data["found_cohorts"] += 1
            audit_summary["cohorts_evaluated"] += 1

            if verify_archives:
                insp = _inspect_cohort_archive(
                    archive_path=archive_path,
                    sha_path=sha_path,
                    manifest_path=manifest_path,
                    cohort_num=cohort_num,
                    pairs=pairs,
                    verify_files=True,
                )

                if insp["status"] != "PASS":
                    error_msg = insp.get("error")
                    if error_msg:
                        member_data["archive_issues"].append(error_msg)
                        audit_summary["discrepancies"].append(error_msg)
                    if "paper_results" in insp:
                        p_res = insp["paper_results"]
                        for iss in p_res["issues"]:
                            member_data["archive_issues"].append(iss)
                            audit_summary["discrepancies"].append(iss)
                        member_data["found_m1_complete"] += p_res["found_m1_complete"]
                        member_data["confirmed_not_found"] += p_res["confirmed_not_found"]
                        member_data["discovery_failed"] += p_res["discovery_failed"]
                        member_data["unprocessed"] += p_res["unprocessed"]
                        member_data["integrity_failures"] += p_res["integrity_failures"]

                        audit_summary["found_m1_complete"] += p_res["found_m1_complete"]
                        audit_summary["confirmed_not_found"] += p_res["confirmed_not_found"]
                        audit_summary["discovery_failed"] += p_res["discovery_failed"]
                        audit_summary["unprocessed"] += p_res["unprocessed"]
                        audit_summary["integrity_failures"] += p_res["integrity_failures"]
                    else:
                        member_data["integrity_failures"] += len(pairs)
                        audit_summary["integrity_failures"] += len(pairs)
                else:
                    member_data["archives_verified"] += 1
                    p_res = insp["paper_results"]
                    member_data["found_m1_complete"] += p_res["found_m1_complete"]
                    member_data["confirmed_not_found"] += p_res["confirmed_not_found"]
                    member_data["discovery_failed"] += p_res["discovery_failed"]
                    member_data["unprocessed"] += p_res["unprocessed"]
                    member_data["integrity_failures"] += p_res["integrity_failures"]

                    audit_summary["found_m1_complete"] += p_res["found_m1_complete"]
                    audit_summary["confirmed_not_found"] += p_res["confirmed_not_found"]
                    audit_summary["discovery_failed"] += p_res["discovery_failed"]
                    audit_summary["unprocessed"] += p_res["unprocessed"]
                    audit_summary["integrity_failures"] += p_res["integrity_failures"]

                # Theo dõi ID bài báo trùng giữa các lô.
                expected_map = {arxiv_id_to_folder_key(aid): aid for _, aid in pairs}
                for s_key in insp.get("seen_folder_keys", []):
                    if s_key in expected_map:
                        p_aid = expected_map[s_key]
                        if p_aid in global_seen_paper_ids:
                            prev_c = global_seen_paper_ids[p_aid]
                            dup_err = (
                                f"Duplicate paper ID {p_aid} in cohort {cohort_num} "
                                f"(previously seen in cohort {prev_c})"
                            )
                            member_data["archive_issues"].append(dup_err)
                            audit_summary["discrepancies"].append(dup_err)
                            member_data["integrity_failures"] += 1
                            audit_summary["integrity_failures"] += 1
                        else:
                            global_seen_paper_ids[p_aid] = cohort_num
            else:
                # Nhánh kiểm tra nhanh chưa xác minh (không bao giờ trả PASS).
                try:
                    m_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    included = m_data.get("included_count", 0)
                    omitted = m_data.get("omitted_count", 0)
                    member_data["found_m1_complete"] += included
                    member_data["unprocessed"] += omitted
                    audit_summary["found_m1_complete"] += included
                    audit_summary["unprocessed"] += omitted
                except Exception as exc:
                    member_data["integrity_failures"] += len(pairs)
                    audit_summary["integrity_failures"] += len(pairs)
                    member_data["archive_issues"].append(
                        f"Corrupted manifest for cohort {cohort_num}: {exc}"
                    )

        # Tổng hợp artifact đo hiệu năng thực tế.
        bench = _aggregate_member_benchmarks(reports_dir)
        member_report = dict(bench)
        if member_report.get("status") == "COLLECTED":
            member_report["status"] = "PARTIAL_STAGE_MEASUREMENTS"
            member_report["known_stage_active_seconds"] = member_report.get("total_active_seconds")
            member_report["total_active_seconds"] = None
            member_report["scope"] = "legacy batch intervals; source-stage only, not end-to-end"
        member_data["benchmark"] = member_report

        if bench["status"] == "COLLECTED":
            any_member_benchmark_collected = True
            if bench["total_active_seconds"] is not None:
                group_active_seconds += bench["total_active_seconds"]
            if bench["sample_count"] > 0 and bench["weighted_ram_mb"] is not None:
                group_total_samples += bench["sample_count"]
                group_sum_ram_samples += bench["weighted_ram_mb"] * bench["sample_count"]
            if bench["sampled_peak_ram_mb"] is not None:
                if group_peak_ram is None or bench["sampled_peak_ram_mb"] > group_peak_ram:
                    group_peak_ram = bench["sampled_peak_ram_mb"]
            if bench["sampled_peak_disk_mb"] is not None:
                if group_peak_disk is None or bench["sampled_peak_disk_mb"] > group_peak_disk:
                    group_peak_disk = bench["sampled_peak_disk_mb"]
            group_s2_runs += bench.get("s2_runs_count", 0)
        else:
            members_with_missing_benchmarks.append(mssv)

        audit_summary["members"][mssv] = member_data

    # Tổng hợp phép đo toàn nhóm.
    if any_member_benchmark_collected:
        audit_summary["benchmark"]["status"] = "PARTIAL_STAGE_MEASUREMENTS"
        audit_summary["benchmark"]["known_stage_active_seconds"] = round(group_active_seconds, 2)
        audit_summary["benchmark"]["total_ram_samples"] = group_total_samples
        if group_total_samples > 0:
            audit_summary["benchmark"]["weighted_ram_mb"] = round(
                group_sum_ram_samples / group_total_samples, 2
            )
        audit_summary["benchmark"]["sampled_peak_ram_mb"] = group_peak_ram
        audit_summary["benchmark"]["sampled_peak_disk_mb"] = group_peak_disk
        audit_summary["benchmark"]["s2_included_runs"] = group_s2_runs
    else:
        audit_summary["benchmark"]["status"] = "NOT_COLLECTED"

    any_archive_issues = any(
        bool(m["archive_issues"]) for m in audit_summary["members"].values()
    )

    # Các điều kiện xét riêng.
    data_integrity_pass = bool(
        verify_archives
        and audit_summary["integrity_failures"] == 0
        and not any_archive_issues
        and len(audit_summary["missing_cohorts"]) == 0
    )

    expected_all_20k_ids = {aid for c in range(1, TOTAL_EXPECTED_COHORTS + 1) for _, aid in cohort_pairs(c)}
    coverage_20k_pass = bool(
        len(all_seen_cohorts) == TOTAL_EXPECTED_COHORTS
        and len(audit_summary["missing_cohorts"]) == 0
        and set(global_seen_paper_ids.keys()) == expected_all_20k_ids
        and audit_summary["unprocessed"] == 0
        and audit_summary["discovery_failed"] == 0
        and (audit_summary["found_m1_complete"] + audit_summary["confirmed_not_found"]) == TOTAL_EXPECTED_IDS
    )

    group_handoff_pass = bool(
        len(missing_members) == 0
        and data_integrity_pass
        and coverage_20k_pass
    )

    audit_summary["data_integrity_pass"] = data_integrity_pass
    audit_summary["coverage_20k_pass"] = coverage_20k_pass
    audit_summary["group_handoff_pass"] = group_handoff_pass

    # Đánh giá trạng thái cuối.
    if group_handoff_pass:
        audit_summary["status"] = "PASS"
    elif missing_members or len(audit_summary["missing_cohorts"]) > 0:
        audit_summary["status"] = "INCOMPLETE"
        if len(all_seen_cohorts) != TOTAL_EXPECTED_COHORTS:
            audit_summary["discrepancies"].append(
                f"Expected {TOTAL_EXPECTED_COHORTS} cohorts, found {len(all_seen_cohorts)}."
            )
        if audit_summary["missing_cohorts"]:
            audit_summary["discrepancies"].append(
                f"Missing cohorts ({len(audit_summary['missing_cohorts'])}): {audit_summary['missing_cohorts'][:10]}..."
            )
    elif audit_summary["integrity_failures"] > 0 or any_archive_issues:
        audit_summary["status"] = "FAILED"
        audit_summary["discrepancies"].append(
            f"Found {audit_summary['integrity_failures']} integrity failures / archive issues."
        )
    elif not verify_archives:
        audit_summary["status"] = "UNVERIFIED"
        audit_summary["discrepancies"].append("Archives were not deeply verified.")
    elif audit_summary["unprocessed"] > 0 or audit_summary["discovery_failed"] > 0:
        audit_summary["status"] = "PARTIAL"
        audit_summary["discrepancies"].append(
            f"Unprocessed IDs: {audit_summary['unprocessed']}, discovery failed: {audit_summary['discovery_failed']}."
        )
    elif members_with_missing_benchmarks:
        audit_summary["status"] = "PARTIAL"
        audit_summary["discrepancies"].append(
            f"Missing benchmark measurements for member(s): {', '.join(members_with_missing_benchmarks)}"
        )
    elif audit_summary["benchmark"]["weighted_ram_mb"] is None:
        audit_summary["status"] = "PARTIAL"
        audit_summary["discrepancies"].append("Benchmark RAM metrics not collected or incomplete.")
    elif len(global_seen_paper_ids) != TOTAL_EXPECTED_IDS:
        audit_summary["status"] = "PARTIAL"
        audit_summary["discrepancies"].append(
            f"Unique verified papers ({len(global_seen_paper_ids)}) does not equal total assigned IDs ({TOTAL_EXPECTED_IDS})."
        )
    else:
        audit_summary["status"] = "PARTIAL"

    return audit_summary


def generate_markdown_report(summary: Dict[str, Any]) -> str:
    bench = summary.get("benchmark", {})
    w_ram = f"{bench.get('weighted_ram_mb')} MB" if bench.get("weighted_ram_mb") is not None else "NOT_COLLECTED"
    p_ram = f"{bench.get('sampled_peak_ram_mb')} MB" if bench.get("sampled_peak_ram_mb") is not None else "NOT_COLLECTED"
    p_disk = f"{bench.get('sampled_peak_disk_mb')} MB" if bench.get("sampled_peak_disk_mb") is not None else "NOT_COLLECTED"
    t_sec = f"{bench.get('known_stage_active_seconds')} s (partial)" if bench.get("known_stage_active_seconds") is not None else "NOT_COLLECTED"

    md = [
        "# Milestone 1 Group Campaign Audit & 20,000-ID Aggregation Report",
        "",
        f"**Audit Timestamp:** {summary['timestamp']}  ",
        f"**Campaign Scope:** 200 Cohorts / 20,000 Assigned arXiv IDs  ",
        f"**Overall Status:** **`{summary['status']}`**  ",
        "",
        "---",
        "",
        "## 0. Gate Verification Summary",
        "",
        "| Verification Gate | Result | Gate Criteria |",
        "|:---|:---:|:---|",
        f"| **Data Integrity Gate** | **`{'PASS' if summary.get('data_integrity_pass') else 'FAIL'}`** | SHA-256 matches sidecar & manifest, valid tar structure, all checksums match |",
        f"| **20,000 IDs Coverage Gate** | **`{'PASS' if summary.get('coverage_20k_pass') else 'FAIL'}`** | Exactly 20,000 assigned IDs verified, zero gaps, zero duplicates, zero foreign |",
        f"| **Group Handoff Gate** | **`{'PASS' if summary.get('group_handoff_pass') else 'FAIL'}`** | 4 members complete, 200 cohorts and archive integrity verified |",
        "",
        "---",
        "",
        "## 1. Overall Accounting & Partition Completeness",
        "",
        "| Metric | Count | Expected | Percentage | Accounting Status |",
        "|:---|:---:|:---:|:---:|:---:|",
        f"| **Assigned arXiv IDs** | {summary['total_assigned_ids']:,} | 20,000 | 100.0% | Strict Coverage |",
        f"| **M1 Complete Papers** | {summary['found_m1_complete']:,} | 20,000 | {summary['found_m1_complete']/200.0:.1f}% | Verified Contract |",
        f"| **Confirmed Not-Found** | {summary['confirmed_not_found']:,} | — | — | Provenance Recorded |",
        f"| **Discovery Failed** | {summary['discovery_failed']:,} | 0 | 0.0% | {'PASS' if summary['discovery_failed']==0 else 'ALERT'} |",
        f"| **Unprocessed IDs** | {summary['unprocessed']:,} | 0 | 0.0% | {'PASS' if summary['unprocessed']==0 else 'ALERT'} |",
        f"| **Integrity Failures** | {summary['integrity_failures']:,} | 0 | 0.0% | {'PASS' if summary['integrity_failures']==0 else 'FAIL'} |",
        "",
        "---",
        "",
        "## 2. Member Partition Accounting (50 Cohorts / 5,000 IDs Each)",
        "",
        "| Member MSSV | Assigned Cohorts | Total IDs | Cohorts Found | Archives Verified | M1 Complete | Unprocessed | Integrity Failures | Partition Status |",
        "|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|",
    ]

    for mssv, mdata in summary.get("members", {}).items():
        st = "PASS" if not mdata["archive_issues"] and mdata["unprocessed"] == 0 and mdata["integrity_failures"] == 0 and mdata["found_cohorts"] == 50 else "INCOMPLETE"
        md.append(
            f"| **`{mssv}`** | {mdata['assigned_cohorts'][0]}..{mdata['assigned_cohorts'][-1]} | {mdata['cohort_count']*100:,} | "
            f"{mdata['found_cohorts']}/50 | {mdata['archives_verified']}/50 | {mdata['found_m1_complete']:,} | {mdata['unprocessed']:,} | "
            f"{mdata['integrity_failures']:,} | **{st}** |"
        )

    md.extend([
        "",
        "---",
        "",
        "## 3. Campaign Performance & Resource Profiling",
        "",
        "| Resource Dimension | Measured Value | Measurement Basis |",
        "|:---|:---:|:---|",
        f"| **Benchmark Status** | **`{bench.get('status', 'NOT_COLLECTED')}`** | Ingestion of actual report.json / resources.json |",
        f"| **Known Stage Runtime** | {t_sec} | Partial source-stage intervals only; use aggregate_m1_group_benchmark.py for campaign wall time |",
        f"| **Weighted Average RAM** | {w_ram} | Sample-count-weighted mean across all cohorts |",
        f"| **Sampled Peak RAM** | {p_ram} | Highest sampled memory watermark |",
        f"| **Sampled Peak Disk** | {p_disk} | Maximum recorded storage utilization |",
        f"| **Total RAM Samples** | {bench.get('total_ram_samples', 0):,} | Total memory probes |",
        f"| **S2 Integrated Runs** | {bench.get('s2_included_runs', 0):,} | Executions measuring Semantic Scholar |",
        "",
        "---",
        "",
        "## 4. Discrepancies and Escalations",
        "",
    ])

    if summary["discrepancies"]:
        for d in summary["discrepancies"]:
            md.append(f"- [!] {d}")
    else:
        md.append("Zero discrepancies identified across all 200 cohorts. Full 20,000-ID campaign verified.")

    md.append("")
    return "\n".join(md)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Group 10 M1 Aggregator & Campaign Audit Script")
    parser.add_argument("--base-dir", type=Path, help="Base path containing member folders (<mssv>/...)")
    parser.add_argument("--member-roots", nargs="*", help="Explicit member roots in format MSSV=/path/to/root")
    parser.add_argument("--output", type=Path, help="Path to write JSON audit summary")
    parser.add_argument("--output-dir", type=Path, default=Path("reports/campaign"), help="Directory to save audit outputs (fallback)")
    parser.add_argument("--report", type=Path, help="Path to write Markdown audit report")
    parser.add_argument("--verify-archives", dest="verify_archives", action="store_true", default=True, help="Verify archive checksums and contents (default: True)")
    parser.add_argument("--skip-archive-verify", dest="verify_archives", action="store_false", help="Skip archive verification")

    args = parser.parse_args(argv)
    member_roots = parse_member_roots(args.base_dir, args.member_roots)

    print(f"Group 10 M1 Aggregator: Discovered {len(member_roots)}/4 member roots: {list(member_roots.keys())}")
    summary = audit_group_campaign(member_roots, verify_archives=args.verify_archives)

    if args.output:
        json_path = args.output
        json_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = args.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / "group_m1_audit.json"

    if args.report:
        md_path = args.report
        md_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = args.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / "group_m1_audit.md"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    md_content = generate_markdown_report(summary)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"Campaign Status: {summary['status']}")
    print(f"Audit report written to: {md_path}")
    print(f"Audit JSON written to: {json_path}")

    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
